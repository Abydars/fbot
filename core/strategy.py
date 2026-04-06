"""
core/strategy.py — Pullback and Breakout entry strategy engines.

Each strategy class exposes a single async method check_entry() which
performs final validation, calculates SL/TP/lot-size, and returns an
EntrySignal dataclass if all conditions are met.
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

    london_open = config.LONDON_OPEN_HOUR
    london_close = config.LONDON_CLOSE_HOUR
    ny_open = config.NY_OPEN_HOUR
    ny_close = config.NY_CLOSE_HOUR

    in_london = london_open <= hour < london_close
    in_ny     = ny_open     <= hour < ny_close
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

    pip_sz = _pip_size(sym_info)
    tick_size  = sym_info["tick_size"]
    tick_value = sym_info["tick_value"]

    if tick_size == 0:
        return sym_info["volume_min"]

    # Value of 1 pip per 1 lot in account currency
    pip_value_per_lot = (pip_sz / tick_size) * tick_value

    risk_amount = balance * (config.RISK_PER_TRADE / 100)
    raw_lot = risk_amount / (sl_distance_pips * pip_value_per_lot)

    # Respect symbol constraints
    v_min  = sym_info["volume_min"]
    v_max  = min(sym_info["volume_max"], config.MAX_LOT_SIZE)
    v_step = sym_info["volume_step"]

    # Round down to nearest volume_step
    if v_step > 0:
        raw_lot = math.floor(raw_lot / v_step) * v_step

    lot = max(v_min, min(raw_lot, v_max))
    return round(lot, 8)


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
# Pullback Strategy
# ---------------------------------------------------------------------------

class PullbackStrategy:
    def __init__(self, client: MT5Client, state: SharedState):
        self.client = client
        self.state = state

    async def check_entry(self, symbol: str, direction: str) -> Optional[EntrySignal]:
        """
        Check all pullback entry conditions and return EntrySignal or None.
        """
        scanner_result = self.state.scanner_results.get(symbol)
        if scanner_result is None:
            return None

        # 1. Score threshold
        if scanner_result.pullback_score < 70:
            return None

        # 2. No existing position on this symbol
        if self.state.has_position_for(symbol):
            logger.debug(f"PB skip {symbol}: already has open position")
            return None

        # 3. Max open trades
        if self.state.open_position_count() >= config.MAX_OPEN_TRADES:
            logger.debug("PB skip: max open trades reached")
            return None

        # 4. Session filter
        if not _is_trading_session(symbol):
            logger.debug(f"PB skip {symbol}: outside trading session")
            return None

        # 5. Daily drawdown
        if not _check_drawdown(self.state):
            logger.warning("PB skip: daily drawdown limit reached")
            return None

        # Fetch live data
        sym_info = await self.client.get_symbol_info(symbol)
        tick     = await self.client.get_tick(symbol)
        df_m15   = await self.client.get_ohlcv(symbol, config.PRIMARY_TIMEFRAME, 100)

        if not sym_info or not tick or df_m15 is None:
            return None

        pip_sz = _pip_size(sym_info)

        # 6. Spread check
        spread_pips = scanner_result.spread
        max_spread  = config.SYMBOL_MAX_SPREAD.get(symbol, config.DEFAULT_MAX_SPREAD)
        if spread_pips > max_spread:
            logger.debug(f"PB skip {symbol}: spread {spread_pips:.1f}p > {max_spread}p")
            return None

        entry_price = tick["ask"] if direction == "LONG" else tick["bid"]
        atr = scanner_result.atr

        # --- SL calculation ---
        swing_window = df_m15.iloc[-50:]
        if direction == "LONG":
            swing_low = swing_window["low"].min()
            sl_price  = swing_low - (config.SL_ATR_BUFFER * atr)
        else:
            swing_high = swing_window["high"].max()
            sl_price   = swing_high + (config.SL_ATR_BUFFER * atr)

        sl_distance = abs(entry_price - sl_price)
        sl_pips     = sl_distance / pip_sz

        # Minimum SL pips floor
        if sl_pips < config.MIN_SL_PIPS:
            sl_pips  = config.MIN_SL_PIPS
            if direction == "LONG":
                sl_price = entry_price - sl_pips * pip_sz
            else:
                sl_price = entry_price + sl_pips * pip_sz
            sl_distance = sl_pips * pip_sz

        # --- TP calculation (minimum RR) ---
        tp_distance = sl_distance * config.MIN_RR_RATIO
        if direction == "LONG":
            tp_price = entry_price + tp_distance
        else:
            tp_price = entry_price - tp_distance

        # --- Lot size ---
        account = await self.client.get_account_info()
        if not account:
            return None
        balance = account["balance"]
        lot = _calculate_lot(balance, sl_pips, sym_info)

        signal = EntrySignal(
            symbol=symbol,
            direction=direction,
            signal_type="PULLBACK",
            entry_price=round(entry_price, sym_info["digits"]),
            sl_price=round(sl_price, sym_info["digits"]),
            tp_price=round(tp_price, sym_info["digits"]),
            lot_size=lot,
            score=scanner_result.pullback_score,
            comment=f"pb_{scanner_result.fib_level}_{scanner_result.pullback_score}",
        )

        logger.info(
            f"[PULLBACK SIGNAL] {symbol} {direction} "
            f"entry={signal.entry_price} SL={signal.sl_price} "
            f"TP={signal.tp_price} lot={lot} score={signal.score}"
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
        """
        Check all breakout entry conditions and return EntrySignal or None.
        """
        scanner_result = self.state.scanner_results.get(symbol)
        if scanner_result is None:
            return None

        # 1. Score threshold
        if scanner_result.breakout_score < 70:
            return None

        # 2. No existing position
        if self.state.has_position_for(symbol):
            return None

        # 3. Max open trades
        if self.state.open_position_count() >= config.MAX_OPEN_TRADES:
            return None

        # 4. Session filter
        if not _is_trading_session(symbol):
            return None

        # 5. Daily drawdown
        if not _check_drawdown(self.state):
            return None

        # Fetch live data
        sym_info = await self.client.get_symbol_info(symbol)
        tick     = await self.client.get_tick(symbol)
        df_m15   = await self.client.get_ohlcv(symbol, config.PRIMARY_TIMEFRAME, 100)

        if not sym_info or not tick or df_m15 is None or len(df_m15) < 40:
            return None

        pip_sz = _pip_size(sym_info)

        # 6. Spread check
        spread_pips = scanner_result.spread
        max_spread  = config.SYMBOL_MAX_SPREAD.get(symbol, config.DEFAULT_MAX_SPREAD)
        if spread_pips > max_spread:
            return None

        # Identify breakout levels
        history_candles  = df_m15.iloc[-38:-8]
        consol_candles   = df_m15.iloc[-9:-1]
        resistance       = history_candles["close"].max()
        support          = history_candles["close"].min()
        consol_range     = consol_candles["high"].max() - consol_candles["low"].min()

        current_price = tick["ask"] if direction == "LONG" else tick["bid"]

        # 7. Price has not retraced more than 50% back into consolidation
        if direction == "LONG":
            retracement_limit = resistance - consol_range * 0.5
            if current_price < retracement_limit:
                logger.debug(f"BO skip {symbol}: price retraced back into range")
                return None
            breakout_level = resistance + config.BREAKOUT_ENTRY_OFFSET_PIPS * pip_sz
        else:
            retracement_limit = support + consol_range * 0.5
            if current_price > retracement_limit:
                logger.debug(f"BO skip {symbol}: price retraced back into range")
                return None
            breakout_level = support - config.BREAKOUT_ENTRY_OFFSET_PIPS * pip_sz

        # SL = opposite end of consolidation range
        if direction == "LONG":
            sl_price = consol_candles["low"].min() - scanner_result.atr * 0.3
        else:
            sl_price = consol_candles["high"].max() + scanner_result.atr * 0.3

        sl_distance = abs(breakout_level - sl_price)
        sl_pips     = sl_distance / pip_sz

        if sl_pips < config.MIN_SL_PIPS:
            sl_pips = config.MIN_SL_PIPS
            if direction == "LONG":
                sl_price = breakout_level - sl_pips * pip_sz
            else:
                sl_price = breakout_level + sl_pips * pip_sz
            sl_distance = sl_pips * pip_sz

        # TP = entry + range * 1.5
        tp_distance = consol_range * 1.5
        if direction == "LONG":
            tp_price = breakout_level + tp_distance
        else:
            tp_price = breakout_level - tp_distance

        # Enforce minimum RR
        if tp_distance < sl_distance * config.MIN_RR_RATIO:
            tp_distance = sl_distance * config.MIN_RR_RATIO
            tp_price = (
                breakout_level + tp_distance if direction == "LONG"
                else breakout_level - tp_distance
            )

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
            f"[BREAKOUT SIGNAL] {symbol} {direction} "
            f"entry={signal.entry_price} SL={signal.sl_price} "
            f"TP={signal.tp_price} lot={lot} score={signal.score}"
        )
        return signal
