"""
core/strategy.py — Pullback and Breakout entry strategy engines.

SL/TP placement is driven entirely by market structure (pivot highs/lows),
not by fixed config multipliers. The bot finds the nearest swing level beyond
the entry to place SL, and the next significant opposing level for TP.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

import config
from core.mt5_client import MT5Client
from core.state import SharedState


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class EntrySignal:
    symbol: str
    direction: str          # "LONG" | "SHORT"
    signal_type: str        # "PULLBACK" | "BREAKOUT"
    entry_price: float      # approximate (market) or exact (limit order)
    sl_price: float
    tp_price: float
    lot_size: float
    score: int
    comment: str = ""
    use_limit_order: bool = False  # True → stop-limit, False → market


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_trading_session(symbol: str) -> bool:
    """Return True if we are inside an allowed trading session."""
    now = datetime.now(timezone.utc)
    hour = now.hour

    in_london = config.LONDON_OPEN_HOUR <= hour < config.LONDON_CLOSE_HOUR
    in_ny     = config.NY_OPEN_HOUR     <= hour < config.NY_CLOSE_HOUR
    in_session = in_london or in_ny

    if not in_session and symbol in config.ASIAN_SESSION_SYMBOLS:
        return True   # Gold/JPY pairs allowed outside main sessions

    return in_session


def _pip_size(sym_info: dict) -> float:
    """Return the pip size for a symbol (0.0001 for most FX, 0.01 for JPY pairs)."""
    digits = sym_info["digits"]
    point  = sym_info["point"]
    return point * 10 if digits in (3, 5) else point


def _calculate_lot(
    balance: float,
    sl_distance_pips: float,
    sym_info: dict,
) -> float:
    """
    Calculate lot size based on fixed fractional risk.

    risk_amount = balance * RISK_PER_TRADE%
    lot         = risk_amount / (sl_pips * pip_value_per_lot)
    """
    if sl_distance_pips <= 0:
        return sym_info["volume_min"]

    pip_sz         = _pip_size(sym_info)
    tick_size      = sym_info["tick_size"]
    tick_value     = sym_info["tick_value"]

    if tick_size == 0:
        return sym_info["volume_min"]

    pip_value_per_lot = (pip_sz / tick_size) * tick_value
    risk_amount       = balance * (config.RISK_PER_TRADE / 100)
    raw_lot           = risk_amount / (sl_distance_pips * pip_value_per_lot)

    v_min  = sym_info["volume_min"]
    v_max  = min(sym_info["volume_max"], config.MAX_LOT_SIZE)
    v_step = sym_info["volume_step"]

    if v_step > 0:
        raw_lot = math.floor(raw_lot / v_step) * v_step

    return round(max(v_min, min(raw_lot, v_max)), 8)


def _check_drawdown(state: SharedState) -> bool:
    """Return True if daily drawdown is within acceptable limits."""
    account = state.account
    if account.day_start_balance <= 0:
        return True
    drawdown_pct = (
        (account.day_start_balance - account.equity)
        / account.day_start_balance * 100
    )
    return drawdown_pct < config.MAX_DAILY_DRAWDOWN_PCT


# ---------------------------------------------------------------------------
# Market-structure SL/TP helpers
# ---------------------------------------------------------------------------

def _find_pivots(df: pd.DataFrame, window: int = 5) -> tuple[list[float], list[float]]:
    """
    Identify pivot highs and lows in OHLCV data.

    A pivot high is a bar whose 'high' is the highest within [i-window, i+window].
    A pivot low  is a bar whose 'low'  is the lowest  within [i-window, i+window].

    Returns (pivot_highs, pivot_lows) — each a list of prices, oldest first.
    """
    pivot_highs: list[float] = []
    pivot_lows:  list[float] = []
    n = len(df)
    for i in range(window, n - window):
        hi = df["high"].iloc[i]
        lo = df["low"].iloc[i]
        window_slice_hi = df["high"].iloc[i - window: i + window + 1]
        window_slice_lo = df["low"].iloc[i - window: i + window + 1]
        if hi == window_slice_hi.max():
            pivot_highs.append(hi)
        if lo == window_slice_lo.min():
            pivot_lows.append(lo)
    return pivot_highs, pivot_lows


def _structure_sl_tp(
    df: pd.DataFrame,
    direction: str,
    entry: float,
    atr: float,
    pip_sz: float,
) -> tuple[float, float]:
    """
    Derive SL and TP from market structure.

    SL is placed just beyond the nearest swing level that *opposes* the trade
    (swing low for LONG, swing high for SHORT), with a small ATR buffer.

    TP targets the nearest swing level *in the trade direction* (swing high
    for LONG, swing low for SHORT).

    Fallbacks when no suitable pivot exists:
      SL → 1.5 × ATR from entry (minimum MIN_SL_PIPS enforced afterward)
      TP → 2 × SL distance projected from entry
    """
    pivot_highs, pivot_lows = _find_pivots(df, window=5)

    # Minimum distance so SL/TP are meaningfully separated from entry
    min_dist = config.MIN_SL_PIPS * pip_sz
    # Small buffer placed beyond the pivot so the stop isn't hit by normal wick noise
    buffer = atr * 0.15

    if direction == "LONG":
        # --- SL: nearest pivot low strictly below (entry - min_dist) ---
        sl_candidates = [p for p in pivot_lows if p < entry - min_dist]
        if sl_candidates:
            sl_base  = max(sl_candidates)          # closest pivot low below entry
            sl_price = sl_base - buffer
        else:
            sl_price = entry - max(min_dist, atr * 1.5)

        # --- TP: nearest pivot high strictly above entry ---
        tp_candidates = [p for p in pivot_highs if p > entry + min_dist]
        if tp_candidates:
            tp_price = min(tp_candidates)          # nearest resistance above
        else:
            tp_price = entry + abs(entry - sl_price) * 2.0

    else:  # SHORT
        # --- SL: nearest pivot high strictly above (entry + min_dist) ---
        sl_candidates = [p for p in pivot_highs if p > entry + min_dist]
        if sl_candidates:
            sl_base  = min(sl_candidates)          # closest pivot high above entry
            sl_price = sl_base + buffer
        else:
            sl_price = entry + max(min_dist, atr * 1.5)

        # --- TP: nearest pivot low strictly below entry ---
        tp_candidates = [p for p in pivot_lows if p < entry - min_dist]
        if tp_candidates:
            tp_price = max(tp_candidates)          # nearest support below
        else:
            tp_price = entry - abs(sl_price - entry) * 2.0

    return sl_price, tp_price


# ---------------------------------------------------------------------------
# Pullback Strategy
# ---------------------------------------------------------------------------

class PullbackStrategy:
    def __init__(self, client: MT5Client, state: SharedState):
        self.client = client
        self.state = state

    async def check_entry(self, symbol: str, direction: str) -> Optional[EntrySignal]:
        scanner_result = self.state.scanner_results.get(symbol)
        if scanner_result is None:
            return None

        if scanner_result.pullback_score < 70:
            return None
        if self.state.has_position_for(symbol):
            logger.debug(f"PB skip {symbol}: already has open position")
            return None
        if self.state.open_position_count() >= config.MAX_OPEN_TRADES:
            logger.debug("PB skip: max open trades reached")
            return None
        if not _is_trading_session(symbol):
            logger.debug(f"PB skip {symbol}: outside trading session")
            return None
        if not _check_drawdown(self.state):
            logger.warning("PB skip: daily drawdown limit reached")
            return None

        sym_info = await self.client.get_symbol_info(symbol)
        tick     = await self.client.get_tick(symbol)
        df       = await self.client.get_ohlcv(symbol, config.PRIMARY_TIMEFRAME, 150)

        if not sym_info or not tick or df is None or len(df) < 20:
            return None

        pip_sz = _pip_size(sym_info)

        # Spread check
        spread_pips = scanner_result.spread
        max_spread  = config.SYMBOL_MAX_SPREAD.get(symbol, config.DEFAULT_MAX_SPREAD)
        if spread_pips > max_spread:
            logger.debug(f"PB skip {symbol}: spread {spread_pips:.1f}p > {max_spread}p")
            return None

        entry = tick["ask"] if direction == "LONG" else tick["bid"]
        atr   = scanner_result.atr

        # --- SL / TP from market structure ---
        sl_price, tp_price = _structure_sl_tp(df, direction, entry, atr, pip_sz)

        sl_distance = abs(entry - sl_price)
        sl_pips     = sl_distance / pip_sz

        # Enforce minimum SL floor
        if sl_pips < config.MIN_SL_PIPS:
            sl_pips     = config.MIN_SL_PIPS
            sl_distance = sl_pips * pip_sz
            sl_price    = entry - sl_distance if direction == "LONG" else entry + sl_distance

        # Risk/reward check — skip if natural structure gives worse than 1:1
        tp_distance = abs(tp_price - entry)
        rr = tp_distance / sl_distance if sl_distance > 0 else 0
        if rr < 1.0:
            logger.debug(
                f"PB skip {symbol}: structure RR {rr:.2f} < 1.0  "
                f"(SL {sl_pips:.1f}p  TP {tp_distance/pip_sz:.1f}p)"
            )
            return None

        account = await self.client.get_account_info()
        if not account:
            return None
        lot = _calculate_lot(account["balance"], sl_pips, sym_info)

        signal = EntrySignal(
            symbol=symbol,
            direction=direction,
            signal_type="PULLBACK",
            entry_price=round(entry, sym_info["digits"]),
            sl_price=round(sl_price, sym_info["digits"]),
            tp_price=round(tp_price, sym_info["digits"]),
            lot_size=lot,
            score=scanner_result.pullback_score,
            comment=f"pb_{scanner_result.fib_level}_{scanner_result.pullback_score}",
        )

        logger.info(
            f"[PULLBACK] {symbol} {direction} "
            f"entry={signal.entry_price} SL={signal.sl_price} ({sl_pips:.1f}p) "
            f"TP={signal.tp_price} ({tp_distance/pip_sz:.1f}p) RR={rr:.2f} lot={lot}"
        )
        return signal


# ---------------------------------------------------------------------------
# Breakout Strategy
# ---------------------------------------------------------------------------

class BreakoutStrategy:
    def __init__(self, client: MT5Client, state: SharedState):
        self.client = client
        self.state = state

    async def check_entry(self, symbol: str, direction: str) -> Optional[EntrySignal]:
        scanner_result = self.state.scanner_results.get(symbol)
        if scanner_result is None:
            return None

        if scanner_result.breakout_score < 70:
            return None
        if self.state.has_position_for(symbol):
            return None
        if self.state.open_position_count() >= config.MAX_OPEN_TRADES:
            return None
        if not _is_trading_session(symbol):
            return None
        if not _check_drawdown(self.state):
            return None

        sym_info = await self.client.get_symbol_info(symbol)
        tick     = await self.client.get_tick(symbol)
        df       = await self.client.get_ohlcv(symbol, config.PRIMARY_TIMEFRAME, 150)

        if not sym_info or not tick or df is None or len(df) < 50:
            return None

        pip_sz = _pip_size(sym_info)

        spread_pips = scanner_result.spread
        max_spread  = config.SYMBOL_MAX_SPREAD.get(symbol, config.DEFAULT_MAX_SPREAD)
        if spread_pips > max_spread:
            return None

        # Identify the consolidation zone (last 8 completed bars)
        # and the history used to find breakout level (bars -38 to -8)
        consol = df.iloc[-9:-1]
        history = df.iloc[-38:-8]

        resistance = history["close"].max()
        support    = history["close"].min()

        current = tick["ask"] if direction == "LONG" else tick["bid"]

        # Price must still be near the breakout level (not retraced >50% into range)
        consol_range = consol["high"].max() - consol["low"].min()
        if direction == "LONG":
            if current < resistance - consol_range * 0.5:
                logger.debug(f"BO skip {symbol}: price retraced back into range")
                return None
            breakout_level = resistance + config.BREAKOUT_ENTRY_OFFSET_PIPS * pip_sz
        else:
            if current > support + consol_range * 0.5:
                logger.debug(f"BO skip {symbol}: price retraced back into range")
                return None
            breakout_level = support - config.BREAKOUT_ENTRY_OFFSET_PIPS * pip_sz

        atr = scanner_result.atr

        # --- SL / TP from market structure relative to breakout level ---
        # For breakout, we derive structure levels from the full dataset
        # but enforce that SL sits within the consolidation (behind the break)
        sl_price, tp_price = _structure_sl_tp(df, direction, breakout_level, atr, pip_sz)

        # Override SL if structure placed it too close — must be inside/below consol
        if direction == "LONG":
            consol_floor = consol["low"].min() - atr * 0.2
            sl_price = min(sl_price, consol_floor)   # SL no higher than consol floor
        else:
            consol_ceil  = consol["high"].max() + atr * 0.2
            sl_price = max(sl_price, consol_ceil)    # SL no lower than consol ceiling

        sl_distance = abs(breakout_level - sl_price)
        sl_pips     = sl_distance / pip_sz

        if sl_pips < config.MIN_SL_PIPS:
            sl_pips     = config.MIN_SL_PIPS
            sl_distance = sl_pips * pip_sz
            sl_price = (
                breakout_level - sl_distance if direction == "LONG"
                else breakout_level + sl_distance
            )

        tp_distance = abs(tp_price - breakout_level)
        rr = tp_distance / sl_distance if sl_distance > 0 else 0
        if rr < 1.0:
            logger.debug(
                f"BO skip {symbol}: structure RR {rr:.2f} < 1.0  "
                f"(SL {sl_pips:.1f}p  TP {tp_distance/pip_sz:.1f}p)"
            )
            return None

        account = await self.client.get_account_info()
        if not account:
            return None
        lot = _calculate_lot(account["balance"], sl_pips, sym_info)

        signal = EntrySignal(
            symbol=symbol,
            direction=direction,
            signal_type="BREAKOUT",
            entry_price=round(breakout_level, sym_info["digits"]),
            sl_price=round(sl_price, sym_info["digits"]),
            tp_price=round(tp_price, sym_info["digits"]),
            lot_size=lot,
            score=scanner_result.breakout_score,
            comment=f"bo_{scanner_result.breakout_score}",
            use_limit_order=True,
        )

        logger.info(
            f"[BREAKOUT] {symbol} {direction} "
            f"entry={signal.entry_price} SL={signal.sl_price} ({sl_pips:.1f}p) "
            f"TP={signal.tp_price} ({tp_distance/pip_sz:.1f}p) RR={rr:.2f} lot={lot}"
        )
        return signal
