"""
core/scanner.py — Background symbol scanner.

Symbols are auto-discovered from MT5 Market Watch on every scan cycle —
no watchlist config needed. The user controls the symbol universe by
adding/removing symbols in their MT5 terminal.

Spread filter is adaptive: skip if spread > 30 % of the current ATR
(this handles the wide range from tight forex to volatile crypto).
"""

import asyncio
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from loguru import logger

import config
from core.mt5_client import MT5Client
from core.state import SharedState, ScannerResult

# ---------------------------------------------------------------------------
# Internal constants (not user-configurable)
# ---------------------------------------------------------------------------
_PRIMARY_TF   = "M15"
_TREND_TF     = "H1"
_EMA_FAST     = 20
_EMA_SLOW     = 50
_RSI_PERIOD   = 14
_ATR_PERIOD   = 14
_MIN_SCORE    = 65          # minimum score to emit a SIGNAL alert


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low   = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close  = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _detect_candle_pattern(df: pd.DataFrame) -> tuple[str, str]:
    """Return (pattern_name, direction) for the last closed candle."""
    if len(df) < 3:
        return "", ""

    c  = df.iloc[-2]   # last closed candle
    p  = df.iloc[-3]

    body   = abs(c["close"] - c["open"])
    range_ = c["high"] - c["low"]
    if range_ == 0:
        return "", ""

    lower_shadow = min(c["open"], c["close"]) - c["low"]
    upper_shadow = c["high"] - max(c["open"], c["close"])

    if lower_shadow >= 2 * body and upper_shadow < body and c["close"] > c["open"]:
        return "hammer", "LONG"
    if upper_shadow >= 2 * body and lower_shadow < body and c["close"] < c["open"]:
        return "shooting_star", "SHORT"

    p_body = abs(p["close"] - p["open"])
    if (c["close"] > c["open"] and p["close"] < p["open"]
            and c["open"] <= p["close"] and c["close"] >= p["open"]
            and body > p_body):
        return "bullish_engulfing", "LONG"
    if (c["close"] < c["open"] and p["close"] > p["open"]
            and c["open"] >= p["close"] and c["close"] <= p["open"]
            and body > p_body):
        return "bearish_engulfing", "SHORT"

    if len(df) >= 4:
        pp         = df.iloc[-4]
        p_body_sz  = abs(p["close"] - p["open"])
        p_range    = p["high"] - p["low"]
        if p_range > 0 and p_body_sz / p_range < 0.2:
            if pp["close"] < pp["open"] and c["close"] > c["open"] and c["close"] > pp["open"]:
                return "morning_star", "LONG"
            if pp["close"] > pp["open"] and c["close"] < c["open"] and c["close"] < pp["open"]:
                return "evening_star", "SHORT"

    return "", ""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_pullback(
    df_m15: pd.DataFrame,
    df_h1: pd.DataFrame,
    symbol: str,
) -> tuple[int, str, str, str]:
    """Return (score 0-100, direction, trend, fib_level)."""
    score     = 0
    direction = ""
    trend     = "NEUTRAL"
    fib_level = ""

    if len(df_m15) < 55 or len(df_h1) < 55:
        return 0, "", trend, fib_level

    # Trend on H1
    h1_ema_fast = _ema(df_h1["close"], _EMA_FAST)
    h1_ema_slow = _ema(df_h1["close"], _EMA_SLOW)
    h1_price    = df_h1["close"].iloc[-1]

    bullish = h1_ema_fast.iloc[-1] > h1_ema_slow.iloc[-1] and h1_price > h1_ema_slow.iloc[-1]
    bearish = h1_ema_fast.iloc[-1] < h1_ema_slow.iloc[-1] and h1_price < h1_ema_slow.iloc[-1]

    if bullish:
        score += 20; direction = "LONG";  trend = "BULLISH"
    elif bearish:
        score += 20; direction = "SHORT"; trend = "BEARISH"
    else:
        return 0, "", "NEUTRAL", fib_level

    # Fibonacci pullback
    sw = df_m15.iloc[-50:]
    swing_high  = sw["high"].max()
    swing_low   = sw["low"].min()
    swing_range = swing_high - swing_low
    if swing_range == 0:
        return score, direction, trend, fib_level

    price = df_m15["close"].iloc[-1]
    fib_levels = {
        "38.2%": swing_high - swing_range * 0.382 if direction == "LONG" else swing_low + swing_range * 0.382,
        "50.0%": swing_high - swing_range * 0.500 if direction == "LONG" else swing_low + swing_range * 0.500,
        "61.8%": swing_high - swing_range * 0.618 if direction == "LONG" else swing_low + swing_range * 0.618,
    }
    tolerance  = price * 0.0015
    fib_scores = {"38.2%": 25, "50.0%": 20, "61.8%": 15}
    for name, level in fib_levels.items():
        if abs(price - level) <= tolerance:
            score += fib_scores[name]; fib_level = name; break

    m15_ema20 = _ema(df_m15["close"], _EMA_FAST)
    if abs(price - m15_ema20.iloc[-1]) <= tolerance:
        score += 15

    rsi_val = _rsi(df_m15["close"], _RSI_PERIOD).iloc[-1]
    if direction == "LONG"  and 30 <= rsi_val <= 45: score += 15
    elif direction == "SHORT" and 55 <= rsi_val <= 70: score += 15

    pattern, pat_dir = _detect_candle_pattern(df_m15)
    if pattern and pat_dir == direction:
        score += 20

    vol_ma20 = df_m15["volume"].rolling(20).mean()
    if vol_ma20.iloc[-1] > 0:
        avg_pb_vol = df_m15["volume"].iloc[-4:-1].mean()
        if avg_pb_vol < vol_ma20.iloc[-1] * 0.70: score += 10
        if df_m15["volume"].iloc[-2] > vol_ma20.iloc[-1]: score += 10

    return min(score, 100), direction, trend, fib_level


def _score_breakout(df_m15: pd.DataFrame) -> tuple[int, str]:
    """Return (score 0-100, direction)."""
    score     = 0
    direction = ""

    if len(df_m15) < 40:
        return 0, ""

    atr   = _atr(df_m15, _ATR_PERIOD)
    price = df_m15["close"].iloc[-1]
    if price == 0:
        return 0, ""

    consol = df_m15.iloc[-9:-1]
    consol_atr = atr.iloc[-9:-1].mean()
    if consol_atr / price < 0.0035: score += 25
    consol_range = consol["high"].max() - consol["low"].min()
    if consol_range / price < 0.005: score += 15

    history   = df_m15.iloc[-38:-8]
    resistance = history["close"].max()
    support    = history["close"].min()
    tol = price * 0.001
    if (abs(history["high"] - resistance) < tol).sum() >= 3: score += 20
    if (abs(history["low"]  - support)    < tol).sum() >= 3: score += 20

    last = df_m15.iloc[-2]
    broke_up   = last["close"] > resistance
    broke_down = last["close"] < support
    candle_rng = last["high"] - last["low"]
    body_pct   = abs(last["close"] - last["open"]) / candle_rng if candle_rng > 0 else 0

    if broke_up:
        score += 25; direction = "LONG"
    elif broke_down:
        score += 25; direction = "SHORT"
    else:
        return score, direction

    if body_pct >= 0.60: score += 10

    vol_ma20 = df_m15["volume"].rolling(20).mean()
    if vol_ma20.iloc[-1] > 0:
        ratio = df_m15["volume"].iloc[-2] / vol_ma20.iloc[-1]
        if ratio >= 1.8:   score += 25
        elif ratio >= 1.2: score += 10

    return min(score, 100), direction


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scanner:
    """
    Continuously scans all tradeable symbols from MT5 Market Watch.
    Add or remove symbols in the MT5 terminal to control what gets scanned.
    """

    def __init__(self, client: MT5Client, state: SharedState):
        self.client = client
        self.state  = state

    async def run(self):
        logger.info("Scanner started — symbols sourced from MT5 Market Watch.")
        while self.state.running:
            try:
                await self._scan_all()
            except Exception as exc:
                logger.exception(f"Scanner error: {exc}")
            await asyncio.sleep(config.SCANNER_INTERVAL)

    async def _scan_all(self):
        symbols = await self.client.get_tradeable_symbols()
        if not symbols:
            logger.warning("Scanner: no tradeable symbols found in MT5 Market Watch.")
            return
        logger.debug(f"Scanner: scanning {len(symbols)} symbols from Market Watch.")
        results = await asyncio.gather(
            *[self._scan_symbol(sym) for sym in symbols],
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logger.debug(f"Scanner: {len(errors)} symbol(s) had errors this cycle.")

    async def _scan_symbol(self, symbol: str):
        df_m15   = await self.client.get_ohlcv(symbol, _PRIMARY_TF, 150)
        df_h1    = await self.client.get_ohlcv(symbol, _TREND_TF, 100)
        tick     = await self.client.get_tick(symbol)
        sym_info = await self.client.get_symbol_info(symbol)

        if df_m15 is None or df_h1 is None or tick is None or sym_info is None:
            return

        pip_sz    = sym_info["point"] * 10 if sym_info["digits"] in (3, 5) else sym_info["point"]
        atr_val   = float(_atr(df_m15, _ATR_PERIOD).iloc[-1])
        atr_pips  = atr_val / pip_sz if pip_sz > 0 else 0

        spread_pips = (sym_info["spread"] * sym_info["point"]) / pip_sz if pip_sz > 0 else 0

        # Adaptive spread filter: skip if spread is > 30 % of ATR
        # (protects against illiquid/wide-spread symbols at any price scale)
        if atr_pips > 0 and spread_pips > atr_pips * 0.30:
            logger.debug(f"Scanner skip {symbol}: spread {spread_pips:.1f}p > 30% ATR {atr_pips:.1f}p")
            return

        rsi_val = float(_rsi(df_m15["close"], _RSI_PERIOD).iloc[-1])
        if np.isnan(rsi_val):
            rsi_val = 50.0

        pb_score, pb_dir, trend, fib_level = _score_pullback(df_m15, df_h1, symbol)
        bo_score, bo_dir                   = _score_breakout(df_m15)

        if pb_score >= bo_score:
            signal_type = "PULLBACK" if pb_score > 0 else ""
            direction   = pb_dir
            top_score   = pb_score
        else:
            signal_type = "BREAKOUT" if bo_score > 0 else ""
            direction   = bo_dir
            top_score   = bo_score

        result = ScannerResult(
            symbol=symbol,
            pullback_score=pb_score,
            breakout_score=bo_score,
            signal_type=signal_type,
            direction=direction,
            trend=trend,
            rsi=rsi_val,
            price=tick["ask"],
            spread=round(spread_pips, 2),
            atr=round(atr_val, 6),
            fib_level=fib_level,
            last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )

        await self.state.update_scanner(result)

        if top_score >= _MIN_SCORE and signal_type:
            msg = (
                f"{symbol} {signal_type} {direction} "
                f"Score:{top_score} RSI:{rsi_val:.1f} Spread:{spread_pips:.1f}p"
            )
            await self.state.add_alert(msg, "SIGNAL")
            logger.info(f"[SIGNAL] {msg}")
