"""
core/scanner.py — Background symbol scanner.

Runs as a continuous async loop every SCANNER_INTERVAL seconds.
For each symbol it fetches OHLCV, computes indicators, scores
pullback and breakout setups, and updates SharedState.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

import config
from core.mt5_client import MT5Client
from core.state import SharedState, ScannerResult


# ---------------------------------------------------------------------------
# Indicator helpers (pure numpy/pandas — no pandas_ta dependency needed here)
# ---------------------------------------------------------------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low   = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close  = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _detect_candle_pattern(df: pd.DataFrame) -> tuple[str, str]:
    """
    Detect simple candlestick patterns on the last closed candle.
    Returns (pattern_name, direction)  direction = "LONG" | "SHORT" | ""
    """
    if len(df) < 3:
        return "", ""

    c = df.iloc[-2]   # last CLOSED candle (last row is forming)
    p = df.iloc[-3]   # candle before it

    body   = abs(c["close"] - c["open"])
    range_ = c["high"] - c["low"]
    if range_ == 0:
        return "", ""

    body_pct = body / range_

    # --- Hammer / Shooting Star ---
    lower_shadow = min(c["open"], c["close"]) - c["low"]
    upper_shadow = c["high"] - max(c["open"], c["close"])

    if (lower_shadow >= 2 * body and upper_shadow < body
            and c["close"] > c["open"]):
        return "hammer", "LONG"

    if (upper_shadow >= 2 * body and lower_shadow < body
            and c["close"] < c["open"]):
        return "shooting_star", "SHORT"

    # --- Bullish / Bearish Engulfing ---
    p_body = abs(p["close"] - p["open"])
    if (c["close"] > c["open"]         # bullish candle
            and p["close"] < p["open"] # previous bearish
            and c["open"] <= p["close"]
            and c["close"] >= p["open"]
            and body > p_body):
        return "bullish_engulfing", "LONG"

    if (c["close"] < c["open"]         # bearish candle
            and p["close"] > p["open"] # previous bullish
            and c["open"] >= p["close"]
            and c["close"] <= p["open"]
            and body > p_body):
        return "bearish_engulfing", "SHORT"

    # --- Doji-based: Morning / Evening Star (simplified) ---
    if len(df) >= 4:
        pp = df.iloc[-4]
        pp_body = abs(pp["close"] - pp["open"])
        p_body_size = abs(p["close"] - p["open"])
        p_range = p["high"] - p["low"]
        if p_range > 0 and p_body_size / p_range < 0.2:   # doji in middle
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
    """
    Compute pullback score (0-100).
    Returns (score, direction, trend, fib_level)
    """
    score = 0
    direction = ""
    trend = "NEUTRAL"
    fib_level = ""

    if len(df_m15) < 55 or len(df_h1) < 55:
        return 0, "", trend, fib_level

    # --- Step 1: Trend filter on H1 ---
    h1_ema20 = _ema(df_h1["close"], 20)
    h1_ema50 = _ema(df_h1["close"], 50)
    h1_price = df_h1["close"].iloc[-1]

    bullish_trend = (
        h1_ema20.iloc[-1] > h1_ema50.iloc[-1]
        and h1_price > h1_ema50.iloc[-1]
    )
    bearish_trend = (
        h1_ema20.iloc[-1] < h1_ema50.iloc[-1]
        and h1_price < h1_ema50.iloc[-1]
    )

    if bullish_trend:
        score += 20
        direction = "LONG"
        trend = "BULLISH"
    elif bearish_trend:
        score += 20
        direction = "SHORT"
        trend = "BEARISH"
    else:
        return 0, "", "NEUTRAL", fib_level   # no trend → skip

    # --- Step 2: Fibonacci pullback ---
    swing_window = df_m15.iloc[-50:]
    swing_high = swing_window["high"].max()
    swing_low  = swing_window["low"].min()
    swing_range = swing_high - swing_low

    if swing_range == 0:
        return score, direction, trend, fib_level

    price = df_m15["close"].iloc[-1]

    fib_levels = {
        "38.2%": swing_high - swing_range * 0.382 if direction == "LONG" else swing_low + swing_range * 0.382,
        "50.0%": swing_high - swing_range * 0.500 if direction == "LONG" else swing_low + swing_range * 0.500,
        "61.8%": swing_high - swing_range * 0.618 if direction == "LONG" else swing_low + swing_range * 0.618,
    }
    tolerance = price * 0.0015   # 0.15%

    fib_scores = {"38.2%": 25, "50.0%": 20, "61.8%": 15}
    for name, level in fib_levels.items():
        if abs(price - level) <= tolerance:
            score += fib_scores[name]
            fib_level = name
            break

    # EMA20 touch on M15
    m15_ema20 = _ema(df_m15["close"], 20)
    if abs(price - m15_ema20.iloc[-1]) <= tolerance:
        score += 15

    # --- Step 3: RSI ---
    rsi = _rsi(df_m15["close"], 14)
    rsi_val = rsi.iloc[-1]
    if direction == "LONG" and 30 <= rsi_val <= 45:
        score += 15
    elif direction == "SHORT" and 55 <= rsi_val <= 70:
        score += 15

    # --- Step 4: Candlestick pattern ---
    pattern, pat_direction = _detect_candle_pattern(df_m15)
    if pattern and pat_direction == direction:
        score += 20

    # --- Step 5: Volume ---
    vol_ma20 = df_m15["volume"].rolling(20).mean()
    pullback_vols = df_m15["volume"].iloc[-4:-1]  # last 3 candles before current
    if vol_ma20.iloc[-1] > 0:
        avg_pullback_vol = pullback_vols.mean()
        if avg_pullback_vol < vol_ma20.iloc[-1] * 0.70:
            score += 10
        if df_m15["volume"].iloc[-2] > vol_ma20.iloc[-1]:
            score += 10

    return min(score, 100), direction, trend, fib_level


def _score_breakout(
    df_m15: pd.DataFrame,
) -> tuple[int, str]:
    """
    Compute breakout score (0-100).
    Returns (score, direction)
    """
    score = 0
    direction = ""

    if len(df_m15) < 40:
        return 0, ""

    atr = _atr(df_m15, 14)
    atr_val = atr.iloc[-1]
    price = df_m15["close"].iloc[-1]

    if price == 0:
        return 0, ""

    # --- Step 1: Consolidation ---
    consol_candles = df_m15.iloc[-9:-1]  # last 8 closed candles
    consol_atr = atr.iloc[-9:-1].mean()
    if price > 0 and consol_atr / price < 0.0035:
        score += 25

    consol_range = consol_candles["high"].max() - consol_candles["low"].min()
    if price > 0 and consol_range / price < 0.005:
        score += 15

    # --- Step 2: Level identification ---
    history_candles = df_m15.iloc[-38:-8]   # 30 candles before consolidation
    resistance = history_candles["close"].max()
    support    = history_candles["close"].min()

    # Count touches (within 0.1% tolerance)
    tol = price * 0.001
    res_touches = (abs(history_candles["high"] - resistance) < tol).sum()
    sup_touches = (abs(history_candles["low"]  - support)    < tol).sum()
    if res_touches >= 3 or sup_touches >= 3:
        score += 20

    # --- Step 3: Breakout candle ---
    last_closed = df_m15.iloc[-2]   # last fully closed candle
    broke_up   = last_closed["close"] > resistance
    broke_down = last_closed["close"] < support

    candle_range = last_closed["high"] - last_closed["low"]
    candle_body  = abs(last_closed["close"] - last_closed["open"])
    body_pct = candle_body / candle_range if candle_range > 0 else 0

    if broke_up:
        score += 25
        direction = "LONG"
    elif broke_down:
        score += 25
        direction = "SHORT"
    else:
        return score, direction   # no breakout yet

    if body_pct >= 0.60:
        score += 10

    # --- Step 4: Volume ---
    vol_ma20 = df_m15["volume"].rolling(20).mean()
    if vol_ma20.iloc[-1] > 0:
        ratio = df_m15["volume"].iloc[-2] / vol_ma20.iloc[-1]
        if ratio >= 1.8:
            score += 25
        elif ratio >= 1.2:
            score += 10

    return min(score, 100), direction


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scanner:
    """Continuously scans the watchlist and updates SharedState."""

    def __init__(self, client: MT5Client, state: SharedState):
        self.client = client
        self.state = state

    async def run(self):
        logger.info("Scanner started.")
        while self.state.running:
            try:
                await self._scan_all()
            except Exception as exc:
                logger.exception(f"Scanner error: {exc}")
            await asyncio.sleep(config.SCANNER_INTERVAL)

    async def _scan_all(self):
        tasks = [self._scan_symbol(sym) for sym in config.SYMBOLS_WATCHLIST]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            logger.debug(f"Scanner: {len(errors)} symbol(s) had errors.")

    async def _scan_symbol(self, symbol: str):
        # Fetch data
        df_m15 = await self.client.get_ohlcv(symbol, config.PRIMARY_TIMEFRAME, 150)
        df_h1  = await self.client.get_ohlcv(symbol, config.TREND_TIMEFRAME, 100)
        tick   = await self.client.get_tick(symbol)
        sym_info = await self.client.get_symbol_info(symbol)

        if df_m15 is None or df_h1 is None or tick is None or sym_info is None:
            return

        # Spread in pips
        spread_points = sym_info["spread"]
        point = sym_info["point"]
        pip_size = point * 10 if sym_info["digits"] in (3, 5) else point
        spread_pips = (spread_points * point) / pip_size if pip_size > 0 else spread_points

        # ATR in price units
        atr_series = _atr(df_m15, config.ATR_PERIOD)
        atr_val = atr_series.iloc[-1]

        # RSI
        rsi_series = _rsi(df_m15["close"], config.RSI_PERIOD)
        rsi_val = rsi_series.iloc[-1]

        # Scores
        pb_score, pb_dir, trend, fib_level = _score_pullback(df_m15, df_h1, symbol)
        bo_score, bo_dir = _score_breakout(df_m15)

        # Pick dominant signal
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
            rsi=float(rsi_val) if not np.isnan(rsi_val) else 50.0,
            price=tick["ask"],
            spread=round(spread_pips, 2),
            atr=round(float(atr_val), 6),
            fib_level=fib_level,
            last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )

        await self.state.update_scanner(result)

        # Emit alert if score is good enough
        if top_score >= config.SCANNER_MIN_SCORE and signal_type:
            alert_msg = (
                f"{symbol} {signal_type} {direction} "
                f"Score:{top_score} RSI:{rsi_val:.1f} Spread:{spread_pips:.1f}p"
            )
            await self.state.add_alert(alert_msg, "SIGNAL")
            logger.info(f"[SIGNAL] {alert_msg}")
