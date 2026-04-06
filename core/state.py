"""
core/state.py — Thread-safe shared in-memory state for the bot.

All async tasks (scanner, order manager, WS broadcaster) read/write here.
Use asyncio.Lock for mutation, plain reads are fine for dicts/lists in CPython.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ScannerResult:
    symbol: str = ""
    pullback_score: int = 0
    breakout_score: int = 0
    signal_type: str = ""      # "PULLBACK" | "BREAKOUT" | ""
    direction: str = ""        # "LONG" | "SHORT" | ""
    trend: str = ""            # "BULLISH" | "BEARISH" | "NEUTRAL"
    rsi: float = 0.0
    price: float = 0.0
    spread: float = 0.0
    atr: float = 0.0
    fib_level: str = ""
    last_updated: str = ""
    last_bar_close: float = 0.0   # close of last COMPLETED candle (spike-filtered)

    def score(self) -> int:
        return max(self.pullback_score, self.breakout_score)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "pullback_score": self.pullback_score,
            "breakout_score": self.breakout_score,
            "signal_type": self.signal_type,
            "direction": self.direction,
            "trend": self.trend,
            "rsi": round(self.rsi, 2),
            "price": self.price,
            "spread": round(self.spread, 2),
            "atr": self.atr,
            "fib_level": self.fib_level,
            "last_updated": self.last_updated,
            "score": self.score(),
        }


@dataclass
class PositionState:
    ticket: int = 0
    symbol: str = ""
    direction: str = ""        # "LONG" | "SHORT"
    signal_type: str = ""      # "PULLBACK" | "BREAKOUT"
    lot_size: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    pnl_pips: float = 0.0
    pnl_currency: float = 0.0
    open_time: str = ""
    comment: str = ""
    # Trailing-stop tracking
    best_price: float = 0.0    # highest seen for LONG, lowest for SHORT
    breakeven_done: bool = False

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "signal_type": self.signal_type,
            "lot_size": self.lot_size,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "pnl_pips": round(self.pnl_pips, 1),
            "pnl_currency": round(self.pnl_currency, 2),
            "open_time": self.open_time,
            "comment": self.comment,
        }


@dataclass
class TradeRecord:
    ticket: int = 0
    symbol: str = ""
    direction: str = ""
    signal_type: str = ""
    lot_size: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    pnl_pips: float = 0.0
    pnl_currency: float = 0.0
    open_time: str = ""
    close_time: str = ""
    status: str = ""           # "CLOSED" | "CANCELLED"
    close_reason: str = ""     # "TP HIT" | "SL HIT" | "STOP OUT" | "MANUAL"

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "direction": self.direction,
            "signal_type": self.signal_type,
            "lot_size": self.lot_size,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "pnl_pips": round(self.pnl_pips, 1),
            "pnl_currency": round(self.pnl_currency, 2),
            "open_time": self.open_time,
            "close_time": self.close_time,
            "status": self.status,
            "close_reason": self.close_reason,
        }


@dataclass
class AccountState:
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    profit: float = 0.0
    drawdown_pct: float = 0.0
    day_start_balance: float = 0.0

    def to_dict(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "margin": round(self.margin, 2),
            "free_margin": round(self.free_margin, 2),
            "profit": round(self.profit, 2),
            "drawdown_pct": round(self.drawdown_pct, 2),
        }


class SharedState:
    """Central in-memory state shared across all async tasks."""

    def __init__(self):
        self._lock = asyncio.Lock()

        # Bot control
        self.running: bool = True
        self.paused: bool = False
        self.mt5_connected: bool = False

        # Per-symbol scanner results  {symbol: ScannerResult}
        self.scanner_results: dict[str, ScannerResult] = {}

        # Open positions  {ticket: PositionState}
        self.open_positions: dict[int, PositionState] = {}

        # Closed trade history (last 100)
        self.trade_history: list[TradeRecord] = []

        # Account snapshot
        self.account: AccountState = AccountState()

        # Alert feed (last 50 entries)
        self.alerts: list[dict] = []

        # Stats
        self.trades_today: int = 0
        self.wins_today: int = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def update_scanner(self, result: ScannerResult):
        async with self._lock:
            self.scanner_results[result.symbol] = result

    async def update_position(self, pos: PositionState):
        async with self._lock:
            self.open_positions[pos.ticket] = pos

    async def remove_position(self, ticket: int):
        async with self._lock:
            self.open_positions.pop(ticket, None)

    async def add_alert(self, msg: str, level: str = "INFO"):
        async with self._lock:
            self.alerts.append({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "message": msg,
                "level": level,
            })
            if len(self.alerts) > 50:
                self.alerts = self.alerts[-50:]

    async def add_trade_history(self, record: TradeRecord):
        async with self._lock:
            self.trade_history.append(record)
            if len(self.trade_history) > 100:
                self.trade_history = self.trade_history[-100:]
            self.trades_today += 1
            if record.pnl_currency > 0:
                self.wins_today += 1

    def win_rate(self) -> float:
        if self.trades_today == 0:
            return 0.0
        return round(self.wins_today / self.trades_today * 100, 1)

    def open_position_count(self) -> int:
        return len(self.open_positions)

    def has_position_for(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.open_positions.values())

    def get_snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot for WebSocket broadcast."""
        scanner_list = sorted(
            [r.to_dict() for r in self.scanner_results.values()],
            key=lambda x: x["score"],
            reverse=True,
        )
        positions_list = [p.to_dict() for p in self.open_positions.values()]
        history_list = [t.to_dict() for t in self.trade_history[-20:]]

        return {
            "type": "full_update",
            "account": self.account.to_dict(),
            "scanner": scanner_list,
            "positions": positions_list,
            "history": history_list,
            "alerts": list(self.alerts[-10:]),
            "bot_status": {
                "running": self.running,
                "paused": self.paused,
                "mt5_connected": self.mt5_connected,
                "trades_today": self.trades_today,
                "win_rate": self.win_rate(),
                "open_count": self.open_position_count(),
            },
        }
