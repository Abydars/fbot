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

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not installed or not available on this platform.")

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

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Initialize connection to MT5 terminal and verify account."""
        if not MT5_AVAILABLE:
            raise RuntimeError(
                "MetaTrader5 package is not installed. "
                "Run: pip install MetaTrader5  (Windows only)"
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

        # Validate watchlist symbols
        await self._validate_symbols()
        return True

    async def disconnect(self):
        """Shut down MT5 connection."""
        if self._connected:
            await asyncio.to_thread(mt5.shutdown)
            self._connected = False
            logger.info("MT5 disconnected.")

    async def _validate_symbols(self):
        """Check which watchlist symbols are available on the server."""
        missing = []
        for sym in config.SYMBOLS_WATCHLIST:
            info = await asyncio.to_thread(mt5.symbol_info, sym)
            if info is None:
                missing.append(sym)
            else:
                # Ensure the symbol is visible in Market Watch
                if not info.visible:
                    await asyncio.to_thread(mt5.symbol_select, sym, True)

        if missing:
            logger.warning(f"Symbols not found on server: {missing}")
        else:
            logger.info(f"All {len(config.SYMBOLS_WATCHLIST)} watchlist symbols verified.")

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
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV candles and return as a pandas DataFrame."""
        tf = TIMEFRAMES.get(timeframe)
        if tf is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None

        rates = await asyncio.to_thread(
            mt5.copy_rates_from_pos, symbol, tf, 0, count
        )
        if rates is None or len(rates) == 0:
            logger.warning(f"No OHLCV data for {symbol} {timeframe}")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        # Keep standard columns
        return df[["open", "high", "low", "close", "volume"]].copy()

    async def get_tick(self, symbol: str) -> Optional[dict]:
        """Return latest bid/ask for a symbol."""
        tick = await asyncio.to_thread(mt5.symbol_info_tick, symbol)
        if tick is None:
            return None
        return {
            "bid":  tick.bid,
            "ask":  tick.ask,
            "last": tick.last,
            "time": datetime.fromtimestamp(tick.time, tz=timezone.utc),
        }

    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Return symbol metadata needed for lot sizing and SL calculation."""
        info = await asyncio.to_thread(mt5.symbol_info, symbol)
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
                "symbol":       p.symbol,
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
                    "symbol":       d.symbol,
                    "type":         "LONG" if d.type == mt5.DEAL_TYPE_BUY else "SHORT",
                    "volume":       d.volume,
                    "price":        d.price,
                    "profit":       d.profit,
                    "swap":         d.swap,
                    "commission":   d.commission,
                    "time":         datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                    "comment":      d.comment,
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
            "symbol":       symbol,
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
            logger.error(f"place_order failed for {symbol}: retcode={code} {comment_txt}")
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
            "symbol":       symbol,
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
        tick = await self.get_tick(pos.symbol)
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
