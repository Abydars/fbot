"""
core/mt5_client.py — MT5 connection, data fetching, and order execution.

IMPORTANT: MetaTrader5 Python library is NOT async-native.
Every mt5.* call is wrapped in asyncio.to_thread() to avoid blocking the
event loop. MT5 terminal must be running on Windows (or Wine on Linux).
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
from loguru import logger

mt5 = None
MT5_AVAILABLE = False

try:
    import MetaTrader5 as mt5  # type: ignore
    MT5_AVAILABLE = True
    logger.info("Using MetaTrader5 (native Windows package).")
except ImportError:
    pass

if not MT5_AVAILABLE:
    try:
        from mt5linux import MetaTrader5 as mt5  # type: ignore
        MT5_AVAILABLE = True
        logger.info("Using mt5linux (Wine/socket bridge for Mac/Linux).")
    except ImportError:
        pass

if not MT5_AVAILABLE:
    logger.warning(
        "No MT5 package found. "
        "Windows: pip install MetaTrader5 | "
        "Mac/Linux: pip install mt5linux  (requires Wine + MT5 server — see README)"
    )

import config

# ---------------------------------------------------------------------------
# Timeframe mapping
# ---------------------------------------------------------------------------
TIMEFRAMES: dict = {}
if MT5_AVAILABLE:
    TIMEFRAMES = {
        "M1":  mt5.TIMEFRAME_M1,
        "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,
        "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
    }


class MT5Client:
    """Async wrapper around the MetaTrader5 Python library."""

    def __init__(self):
        self._connected = False
        self._warned_no_data: set[str] = set()  # suppress repeated OHLCV warnings

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Initialize connection to MT5 terminal and verify account."""
        if not MT5_AVAILABLE:
            raise RuntimeError(
                "No MT5 package available.\n"
                "  Windows : pip install MetaTrader5\n"
                "  Mac/Linux: pip install mt5linux  "
                "(then start the mt5linux server inside Wine — see README)"
            )

        logger.info(
            f"Connecting to MT5 | account={config.EXNESS_ACCOUNT} "
            f"server={config.EXNESS_SERVER} mode={config.TRADING_MODE.upper()}"
        )

        ok = await asyncio.to_thread(
            mt5.initialize,
            login=config.EXNESS_ACCOUNT,
            password=config.EXNESS_PASSWORD,
            server=config.EXNESS_SERVER,
        )

        if not ok:
            err = await asyncio.to_thread(mt5.last_error)
            raise ConnectionError(f"MT5 initialize() failed: {err}")

        account_info = await asyncio.to_thread(mt5.account_info)
        if account_info is None:
            raise ConnectionError("MT5 connected but account_info() returned None.")

        self._connected = True

        # Print startup summary
        logger.success("=" * 60)
        logger.success("  MT5 CONNECTION ESTABLISHED")
        logger.success(f"  Name    : {account_info.name}")
        logger.success(f"  Login   : {account_info.login}")
        logger.success(f"  Server  : {account_info.server}")
        logger.success(f"  Balance : {account_info.balance:.2f} {account_info.currency}")
        logger.success(f"  Equity  : {account_info.equity:.2f} {account_info.currency}")
        logger.success(f"  Leverage: 1:{account_info.leverage}")
        logger.success(
            f"  Type    : {'DEMO' if account_info.trade_mode == 0 else 'LIVE'}"
        )
        # Check AutoTrading is enabled
        terminal_info = await asyncio.to_thread(mt5.terminal_info)
        if terminal_info and not terminal_info.trade_allowed:
            logger.warning(
                "AutoTrading is DISABLED in MT5 terminal! "
                "Click the AutoTrading button (toolbar) or enable via "
                "Tools → Options → Expert Advisors → Allow automated trading. "
                "The bot will run but cannot place orders until it is enabled."
            )
        elif terminal_info and terminal_info.trade_allowed:
            logger.success("  AutoTrading : ENABLED")

        logger.success("=" * 60)

        # Validate mode matches config
        is_demo = account_info.trade_mode == 0
        if config.TRADING_MODE == "demo" and not is_demo:
            logger.warning(
                "Config says DEMO but account is LIVE. "
                "Update TRADING_MODE in config.py or .env"
            )
        elif config.TRADING_MODE == "live" and is_demo:
            logger.warning(
                "Config says LIVE but account is DEMO. "
                "Update TRADING_MODE in config.py or .env"
            )

        # Validate watchlist symbols + sanity-check data access
        await self._validate_symbols()
        await self._check_data_access()
        return True

    async def disconnect(self):
        """Shut down MT5 connection."""
        if self._connected:
            await asyncio.to_thread(mt5.shutdown)
            self._connected = False
            logger.info("MT5 disconnected.")

    # Symbol suffix detected at connect time (e.g. "" | "m" | ".s" | "+")
    _symbol_suffix: str = ""

    async def _detect_symbol_suffix(self) -> str:
        """
        Exness (and some other brokers) append a suffix to symbol names.
        Try common suffixes against EURUSD to find the one this server uses.
        Returns the suffix string (empty string = plain names).
        """
        # Allow manual override from .env / config.py
        if config.SYMBOL_SUFFIX != "":
            logger.info(f"Using forced SYMBOL_SUFFIX='{config.SYMBOL_SUFFIX}' from config")
            return config.SYMBOL_SUFFIX

        candidates = ["", "m", "+", ".s", ".r", "pro", "ecn", ".e"]
        for suffix in candidates:
            probe = f"EURUSD{suffix}"
            info = await asyncio.to_thread(mt5.symbol_info, probe)
            if info is not None:
                if suffix:
                    logger.info(
                        f"Symbol suffix detected: '{suffix}'  "
                        f"(e.g. EURUSD → EURUSD{suffix}). "
                        f"Update SYMBOL_SUFFIX in .env if needed."
                    )
                else:
                    logger.info("Symbol names use no suffix (plain EURUSD etc.)")
                return suffix

        # Nothing matched — dump available symbols to help diagnose
        logger.warning("Could not detect symbol suffix. Dumping available symbols:")
        all_syms = await asyncio.to_thread(mt5.symbols_get)
        if all_syms:
            sample = [s.name for s in all_syms[:30]]
            logger.warning(f"  First 30 symbols on server: {sample}")
            logger.warning(
                "  Set SYMBOL_SUFFIX in .env to match the suffix used above (e.g. SYMBOL_SUFFIX=m)."
            )
        return ""

    def _apply_suffix(self, symbol: str) -> str:
        """Return symbol name with the detected broker suffix applied."""
        return f"{symbol}{self._symbol_suffix}"

    def _strip_suffix(self, symbol: str) -> str:
        """Remove broker suffix to get the canonical watchlist symbol name.
        MT5 returns positions/deals with the suffixed name (e.g. 'AUDUSDm').
        Stripping it gives 'AUDUSD' so downstream calls don't double-apply."""
        if self._symbol_suffix and symbol.endswith(self._symbol_suffix):
            return symbol[: -len(self._symbol_suffix)]
        return symbol

    async def _validate_symbols(self):
        """
        Auto-detect broker symbol suffix, then preload history for all
        visible Market Watch symbols so the first scan cycle has data.
        """
        self._symbol_suffix = await self._detect_symbol_suffix()

        # Collect all visible + tradeable symbols from Market Watch
        all_syms = await asyncio.to_thread(mt5.symbols_get)
        tradeable = [
            s.name for s in (all_syms or [])
            if s.visible and s.trade_mode == 4
        ]
        if not tradeable:
            logger.warning(
                "No tradeable symbols found in MT5 Market Watch. "
                "Add symbols to Market Watch in your MT5 terminal."
            )
            return

        logger.info(
            f"Found {len(tradeable)} tradeable symbols in Market Watch. "
            f"Preloading historical data (this may take 10–30 s)..."
        )
        preload_tasks = [self._preload_symbol(sym) for sym in tradeable]
        await asyncio.gather(*preload_tasks)
        logger.info("Historical data preload complete.")

    async def _preload_symbol(self, symbol: str, retries: int = 8, delay: float = 4.0):
        """Request a small candle slice to trigger MT5 broker data download."""
        tf = TIMEFRAMES.get("H1")
        for attempt in range(1, retries + 1):
            # Try position-based first
            rates = await asyncio.to_thread(
                mt5.copy_rates_from_pos, symbol, tf, 0, 10
            )
            # Fall back to date range (more reliable when market closed / fresh terminal)
            if rates is None or len(rates) == 0:
                date_to   = datetime.now(timezone.utc)
                date_from = date_to - timedelta(days=7)
                rates = await asyncio.to_thread(
                    mt5.copy_rates_range, symbol, tf, date_from, date_to
                )
            if rates is not None and len(rates) > 0:
                logger.debug(f"Preload OK: {symbol}")
                return
            err = await asyncio.to_thread(mt5.last_error)
            logger.debug(f"Preload {symbol} attempt {attempt}/{retries}: {err}")
            await asyncio.sleep(delay)
        logger.warning(f"Could not preload history for {symbol} — will retry during scan.")

    async def _check_data_access(self):
        """
        Quick sanity check: try to fetch 5 H1 bars for EURUSD (with suffix).
        If this fails, log a single clear warning.
        """
        probe = self._apply_suffix("EURUSD")
        tf = TIMEFRAMES.get("H1")
        rates = await asyncio.to_thread(mt5.copy_rates_from_pos, probe, tf, 0, 5)
        if rates is None or len(rates) == 0:
            err = await asyncio.to_thread(mt5.last_error)
            logger.warning("=" * 60)
            logger.warning("  DATA ACCESS CHECK FAILED")
            logger.warning(f"  Probe symbol : {probe}")
            logger.warning(f"  MT5 error    : {err}")
            logger.warning("  Possible causes:")
            logger.warning("  1. mt5linux server not running inside Wine")
            logger.warning("  2. Market closed + no cached history (open a chart in MT5)")
            logger.warning("  3. Symbol not in Market Watch")
            logger.warning("  Bot will keep retrying — scanner results will be empty")
            logger.warning("=" * 60)
        else:
            logger.success(f"Data access OK — {len(rates)} bars fetched for {probe}")

    # ------------------------------------------------------------------
    # Account info
    # ------------------------------------------------------------------

    async def get_account_info(self) -> Optional[dict]:
        """Return current account balance, equity, margin, free_margin, profit."""
        info = await asyncio.to_thread(mt5.account_info)
        if info is None:
            return None
        return {
            "balance":     info.balance,
            "equity":      info.equity,
            "margin":      info.margin,
            "free_margin": info.margin_free,
            "profit":      info.profit,
            "currency":    info.currency,
            "leverage":    info.leverage,
            "name":        info.name,
            "server":      info.server,
            "trade_mode":  "DEMO" if info.trade_mode == 0 else "LIVE",
        }

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def get_symbols(self, group: str = "") -> list[str]:
        """Return all available symbols, optionally filtered by group pattern."""
        if group:
            symbols = await asyncio.to_thread(mt5.symbols_get, group)
        else:
            symbols = await asyncio.to_thread(mt5.symbols_get)
        if symbols is None:
            return []
        return [s.name for s in symbols]

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        count: int = 200,
        retries: int = 4,
        retry_delay: float = 3.0,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV candles and return as a pandas DataFrame.

        Strategy:
        1. Try copy_rates_from_pos (position-based, fastest)
        2. If that fails, fall back to copy_rates_from with an explicit date
           range — more reliable on weekends and fresh terminals where MT5
           hasn't cached recent bars yet.
        """
        tf = TIMEFRAMES.get(timeframe)
        if tf is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None

        actual = self._apply_suffix(symbol)

        for attempt in range(1, retries + 1):
            # --- Method 1: position-based (most common) ---
            rates = await asyncio.to_thread(
                mt5.copy_rates_from_pos, actual, tf, 0, count
            )

            # --- Method 2: date-range fallback ---
            if rates is None or len(rates) == 0:
                date_to   = datetime.now(timezone.utc)
                # Request enough history to cover count bars with margin
                days_back = max(7, count // 24 + 7)
                date_from = date_to - timedelta(days=days_back)
                rates = await asyncio.to_thread(
                    mt5.copy_rates_range, actual, tf, date_from, date_to
                )
                if rates is not None and len(rates) > 0:
                    # Trim to requested count
                    rates = rates[-count:]

            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
                df.set_index("time", inplace=True)
                df.rename(columns={"tick_volume": "volume"}, inplace=True)
                return df[["open", "high", "low", "close", "volume"]].copy()

            err = await asyncio.to_thread(mt5.last_error)
            if attempt < retries:
                logger.debug(
                    f"OHLCV retry {attempt}/{retries} {symbol} {timeframe} "
                    f"— MT5 error: {err}"
                )
                await asyncio.sleep(retry_delay)
            else:
                key = f"{symbol}:{timeframe}"
                if key not in self._warned_no_data:
                    logger.warning(
                        f"No OHLCV data for {symbol} {timeframe} "
                        f"(MT5 error: {err}). Suppressing further warnings for this symbol."
                    )
                    self._warned_no_data.add(key)
        return None

    async def get_tick(self, symbol: str) -> Optional[dict]:
        """Return latest bid/ask for a symbol."""
        tick = await asyncio.to_thread(mt5.symbol_info_tick, self._apply_suffix(symbol))
        if tick is None:
            return None
        return {
            "bid":  tick.bid,
            "ask":  tick.ask,
            "last": tick.last,
            "time": datetime.fromtimestamp(tick.time, tz=timezone.utc),
        }

    async def get_tradeable_symbols(self) -> list[str]:
        """
        Return canonical names of all visible, fully-tradeable symbols from
        the MT5 Market Watch.  The user controls this list directly in MT5 by
        adding/removing symbols from their Market Watch window.
        """
        symbols = await asyncio.to_thread(mt5.symbols_get)
        if not symbols:
            return []
        result = []
        for s in symbols:
            if not s.visible:
                continue
            # SYMBOL_TRADE_MODE_FULL = 4 — skip suspended / close-only symbols
            if s.trade_mode != 4:
                continue
            result.append(self._strip_suffix(s.name))
        # Deduplicate (suffix stripping can theoretically collide)
        return list(dict.fromkeys(result))

    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Return symbol metadata needed for lot sizing and SL calculation."""
        info = await asyncio.to_thread(mt5.symbol_info, self._apply_suffix(symbol))
        if info is None:
            return None
        return {
            "symbol":       info.name,
            "digits":       info.digits,
            "point":        info.point,            # one point (e.g. 0.00001 for EURUSD)
            "tick_size":    info.trade_tick_size,
            "tick_value":   info.trade_tick_value, # value of 1 tick move per 1 lot
            "volume_min":   info.volume_min,
            "volume_max":   info.volume_max,
            "volume_step":  info.volume_step,
            "spread":       info.spread,           # in points
            "currency_profit": info.currency_profit,
        }

    # ------------------------------------------------------------------
    # Positions & history
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[dict]:
        """Return all currently open positions."""
        positions = await asyncio.to_thread(mt5.positions_get)
        if positions is None:
            return []
        result = []
        for p in positions:
            result.append({
                "ticket":       p.ticket,
                "symbol":       self._strip_suffix(p.symbol),   # canonical, no broker suffix
                "type":         "LONG" if p.type == mt5.ORDER_TYPE_BUY else "SHORT",
                "volume":       p.volume,
                "open_price":   p.price_open,
                "current_price": p.price_current,
                "sl":           p.sl,
                "tp":           p.tp,
                "profit":       p.profit,
                "swap":         p.swap,
                "open_time":    datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
                "comment":      p.comment,
            })
        return result

    async def get_trade_history(self, days: int = 7) -> list[dict]:
        """Return closed deals from the past N days."""
        date_from = datetime.now(timezone.utc) - timedelta(days=days)
        date_to   = datetime.now(timezone.utc)
        deals = await asyncio.to_thread(
            mt5.history_deals_get, date_from, date_to
        )
        if deals is None:
            return []
        result = []
        for d in deals:
            if d.entry == mt5.DEAL_ENTRY_OUT:   # only closed legs
                result.append({
                    "ticket":       d.ticket,
                    "order":        d.order,
                    "symbol":       self._strip_suffix(d.symbol),
                    "type":         "LONG" if d.type == mt5.DEAL_TYPE_BUY else "SHORT",
                    "volume":       d.volume,
                    "price":        d.price,
                    "profit":       d.profit,
                    "swap":         d.swap,
                    "commission":   d.commission,
                    "time":         datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                    "comment":      d.comment,
                    "reason":       d.reason,   # MT5 deal reason code
                })
        return result

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str,
        direction: str,       # "LONG" | "SHORT"
        lot: float,
        sl: float,
        tp: float,
        comment: str = "fbot",
    ) -> Optional[dict]:
        """Place a market order. Returns the result dict or None on failure."""
        tick = await self.get_tick(symbol)
        if tick is None:
            logger.error(f"place_order: no tick for {symbol}")
            return None

        order_type = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL
        price = tick["ask"] if direction == "LONG" else tick["bid"]

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       self._apply_suffix(symbol),
            "volume":       lot,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    10,
            "magic":        20240101,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            comment_txt = result.comment if result else ""
            hint = ""
            if code == 10027:
                hint = " → Enable AutoTrading in MT5 toolbar"
            elif code == 10018:
                hint = " → Market is closed"
            elif code == 10019:
                hint = " → Insufficient funds"
            elif code == 10016:
                hint = " → Invalid SL/TP"
            logger.error(f"place_order failed for {symbol}: retcode={code} {comment_txt}{hint}")
            return None

        logger.success(
            f"Order placed | {symbol} {direction} {lot} lots "
            f"ticket={result.order} entry~{price:.5f} SL={sl:.5f} TP={tp:.5f}"
        )
        return {
            "ticket":    result.order,
            "price":     result.price,
            "volume":    result.volume,
            "retcode":   result.retcode,
        }

    async def place_limit_order(
        self,
        symbol: str,
        direction: str,
        lot: float,
        price: float,
        sl: float,
        tp: float,
        comment: str = "fbot-bo",
    ) -> Optional[dict]:
        """Place a stop-limit entry order for breakout strategy."""
        order_type = (
            mt5.ORDER_TYPE_BUY_STOP if direction == "LONG"
            else mt5.ORDER_TYPE_SELL_STOP
        )

        request = {
            "action":       mt5.TRADE_ACTION_PENDING,
            "symbol":       self._apply_suffix(symbol),
            "volume":       lot,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    10,
            "magic":        20240101,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }

        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            logger.error(f"place_limit_order failed for {symbol}: retcode={code}")
            return None

        logger.success(
            f"Stop order placed | {symbol} {direction} {lot} lots "
            f"ticket={result.order} trigger={price:.5f} SL={sl:.5f} TP={tp:.5f}"
        )
        return {"ticket": result.order, "price": price, "volume": lot}

    async def close_position(self, ticket: int) -> bool:
        """Close a specific position by ticket."""
        positions = await asyncio.to_thread(mt5.positions_get, ticket=ticket)
        if not positions:
            logger.warning(f"close_position: ticket {ticket} not found.")
            return False

        pos = positions[0]
        close_type = (
            mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY
            else mt5.ORDER_TYPE_BUY
        )
        tick = await self.get_tick(self._strip_suffix(pos.symbol))
        if tick is None:
            return False

        close_price = tick["bid"] if pos.type == mt5.ORDER_TYPE_BUY else tick["ask"]

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        close_price,
            "deviation":    10,
            "magic":        20240101,
            "comment":      "fbot-close",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            logger.error(f"close_position failed ticket={ticket}: retcode={code}")
            return False

        logger.success(f"Position closed | ticket={ticket}")
        return True

    async def modify_position(self, ticket: int, sl: float, tp: float) -> bool:
        """Modify SL and TP of an open position."""
        positions = await asyncio.to_thread(mt5.positions_get, ticket=ticket)
        if not positions:
            return False

        pos = positions[0]
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   pos.symbol,
            "position": ticket,
            "sl":       sl,
            "tp":       tp,
        }

        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            logger.debug(f"modify_position ticket={ticket}: retcode={code}")
            return False
        return True
