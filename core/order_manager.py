"""
core/order_manager.py — Entry execution, position monitoring, trailing stops.

The OrderManager runs two perpetual async loops:
  1. _strategy_loop  — evaluates scanner signals and fires new entries
  2. monitor_positions — tracks open positions, applies trailing stops
"""

import asyncio
from datetime import datetime, timezone

import aiosqlite
from loguru import logger

import config
from core.mt5_client import MT5Client
from core.state import SharedState, PositionState, TradeRecord, AccountState
from core.strategy import PullbackStrategy, BreakoutStrategy, EntrySignal, _pip_size

# MT5 deal reason codes → human label
_DEAL_REASON = {
    0: "MANUAL",    # client terminal
    1: "MANUAL",    # mobile
    2: "MANUAL",    # web
    3: "BOT",       # EA / expert
    4: "SL HIT",    # stop-loss triggered
    5: "TP HIT",    # take-profit triggered
    6: "STOP OUT",  # margin stop-out
}


def _infer_close_reason(deal: dict | None, pos: "PositionState") -> str:
    """Determine why a position was closed."""
    if deal and "reason" in deal:
        return _DEAL_REASON.get(deal["reason"], "MANUAL")
    # Fallback: compare exit price to known levels
    exit_px = deal["price"] if deal else pos.current_price
    if pos.tp_price and abs(exit_px - pos.tp_price) < abs(exit_px) * 0.0005:
        return "TP HIT"
    if pos.sl_price and abs(exit_px - pos.sl_price) < abs(exit_px) * 0.0005:
        return "SL HIT"
    return "MANUAL"


class OrderManager:
    def __init__(self, client: MT5Client, state: SharedState):
        self.client = client
        self.state = state
        self.pullback = PullbackStrategy(client, state)
        self.breakout = BreakoutStrategy(client, state)
        self._db_path = config.DB_PATH

    # ------------------------------------------------------------------
    # Public entry point — starts all concurrent loops
    # ------------------------------------------------------------------

    async def run(self):
        logger.info("OrderManager started.")
        await self._init_db()
        await self._restore_state()
        await asyncio.gather(
            self._strategy_loop(),
            self._monitor_loop(),
            self._account_loop(),
        )

    # ------------------------------------------------------------------
    # Database initialisation
    # ------------------------------------------------------------------

    async def _init_db(self):
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id              INTEGER PRIMARY KEY,
                    ticket          INTEGER UNIQUE,
                    symbol          TEXT,
                    direction       TEXT,
                    signal_type     TEXT,
                    lot_size        REAL,
                    entry_price     REAL,
                    sl_price        REAL,
                    tp_price        REAL,
                    exit_price      REAL,
                    pnl_pips        REAL,
                    pnl_currency    REAL,
                    open_time       TEXT,
                    close_time      TEXT,
                    status          TEXT,
                    comment         TEXT,
                    close_reason    TEXT DEFAULT ''
                )
            """)
            # Migrate older DBs that don't have close_reason yet
            try:
                await db.execute("ALTER TABLE trades ADD COLUMN close_reason TEXT DEFAULT ''")
            except Exception:
                pass  # column already exists
            await db.execute("""
                CREATE TABLE IF NOT EXISTS scanner_log (
                    id              INTEGER PRIMARY KEY,
                    timestamp       TEXT,
                    symbol          TEXT,
                    pullback_score  INTEGER,
                    breakout_score  INTEGER,
                    direction       TEXT,
                    signal_type     TEXT,
                    action          TEXT
                )
            """)
            await db.commit()
        logger.info(f"Database ready: {self._db_path}")

    # ------------------------------------------------------------------
    # State restoration after restart
    # ------------------------------------------------------------------

    async def _restore_state(self):
        """
        Re-populate SharedState from the DB so the bot resumes seamlessly
        after a restart.

        - Trade history  : last 100 closed trades
        - Daily stats    : trades_today / wins_today from today's closed rows
        - Open positions : rows still marked OPEN in the DB; live price/P&L
                           will be refreshed by the first monitor_positions() cycle
        """
        from datetime import date
        today_prefix = date.today().isoformat()  # "2024-01-15"

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row

            # 1. Closed trade history (oldest → newest so list is in order)
            async with db.execute(
                "SELECT * FROM trades WHERE status='CLOSED' "
                "ORDER BY id DESC LIMIT 100"
            ) as cur:
                rows = await cur.fetchall()

            for row in reversed(rows):
                self.state.trade_history.append(TradeRecord(
                    ticket=row["ticket"],
                    symbol=row["symbol"],
                    direction=row["direction"],
                    signal_type=row["signal_type"] or "",
                    lot_size=row["lot_size"],
                    entry_price=row["entry_price"],
                    exit_price=row["exit_price"] or 0.0,
                    sl_price=row["sl_price"] or 0.0,
                    tp_price=row["tp_price"] or 0.0,
                    pnl_pips=row["pnl_pips"] or 0.0,
                    pnl_currency=row["pnl_currency"] or 0.0,
                    open_time=row["open_time"] or "",
                    close_time=row["close_time"] or "",
                    status=row["status"],
                    close_reason=row["close_reason"] or "",
                ))

            # 2. Today's win/loss counts
            async with db.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN pnl_currency > 0 THEN 1 ELSE 0 END) "
                "FROM trades WHERE status='CLOSED' AND close_time LIKE ?",
                (f"{today_prefix}%",),
            ) as cur:
                row = await cur.fetchone()
            if row and row[0]:
                self.state.trades_today = int(row[0])
                self.state.wins_today   = int(row[1] or 0)

            # 3. Open positions — restore from DB; monitor loop will sync live data
            async with db.execute(
                "SELECT * FROM trades WHERE status='OPEN'"
            ) as cur:
                rows = await cur.fetchall()

            for row in rows:
                pos = PositionState(
                    ticket=row["ticket"],
                    symbol=row["symbol"],
                    direction=row["direction"],
                    signal_type=row["signal_type"] or "",
                    lot_size=row["lot_size"],
                    entry_price=row["entry_price"],
                    current_price=row["entry_price"],   # refreshed on next monitor tick
                    sl_price=row["sl_price"] or 0.0,
                    tp_price=row["tp_price"] or 0.0,
                    open_time=row["open_time"] or "",
                    comment=row["comment"] or "",
                    best_price=row["entry_price"],       # conservative trailing-stop seed
                )
                self.state.open_positions[pos.ticket] = pos

        logger.info(
            f"State restored — history: {len(self.state.trade_history)} trades | "
            f"open: {len(self.state.open_positions)} positions | "
            f"today: {self.state.trades_today} trades ({self.state.wins_today} wins)"
        )

    # ------------------------------------------------------------------
    # Strategy evaluation loop
    # ------------------------------------------------------------------

    async def _strategy_loop(self):
        """Check scanner results and fire entry signals periodically."""
        while self.state.running:
            if not self.state.paused:
                try:
                    await self._evaluate_signals()
                except Exception as exc:
                    logger.exception(f"Strategy loop error: {exc}")
            await asyncio.sleep(config.SCANNER_INTERVAL)

    async def _evaluate_signals(self):
        for sym, result in list(self.state.scanner_results.items()):
            if not result.direction or not result.signal_type:
                continue

            signal: EntrySignal | None = None

            if result.signal_type == "PULLBACK" and result.pullback_score >= 70:
                signal = await self.pullback.check_entry(sym, result.direction)

            elif result.signal_type == "BREAKOUT" and result.breakout_score >= 70:
                signal = await self.breakout.check_entry(sym, result.direction)

            if signal:
                await self.execute_signal(signal)
                # Log to scanner_log
                await self._log_scanner(result, "SIGNAL")
            else:
                if result.score() >= config.SCANNER_MIN_SCORE:
                    await self._log_scanner(result, "WATCHED")

    # ------------------------------------------------------------------
    # Execute a signal
    # ------------------------------------------------------------------

    async def execute_signal(self, signal: EntrySignal):
        """Pre-flight checks → place order → record in DB → update state."""

        # Pre-flight: margin / max trades
        account = await self.client.get_account_info()
        if not account:
            return
        if account["free_margin"] < account["balance"] * 0.1:
            logger.warning("execute_signal: insufficient free margin, skipping.")
            return

        # Place the order
        if signal.use_limit_order:
            result = await self.client.place_limit_order(
                symbol=signal.symbol,
                direction=signal.direction,
                lot=signal.lot_size,
                price=signal.entry_price,
                sl=signal.sl_price,
                tp=signal.tp_price,
                comment=signal.comment,
            )
        else:
            result = await self.client.place_order(
                symbol=signal.symbol,
                direction=signal.direction,
                lot=signal.lot_size,
                sl=signal.sl_price,
                tp=signal.tp_price,
                comment=signal.comment,
            )

        if not result:
            await self.state.add_alert(
                f"ORDER FAILED: {signal.symbol} {signal.direction}", "ERROR"
            )
            return

        ticket = result["ticket"]
        entry  = result.get("price", signal.entry_price)
        now    = datetime.now(timezone.utc).isoformat()

        # Update in-memory state
        pos = PositionState(
            ticket=ticket,
            symbol=signal.symbol,
            direction=signal.direction,
            signal_type=signal.signal_type,
            lot_size=signal.lot_size,
            entry_price=entry,
            current_price=entry,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
            open_time=now,
            comment=signal.comment,
            best_price=entry,
        )
        await self.state.update_position(pos)

        # Persist to DB
        await self._db_insert_trade(ticket, signal, entry, now)

        await self.state.add_alert(
            f"TRADE OPEN: {signal.symbol} {signal.direction} "
            f"{signal.lot_size}L @{entry:.5f} | {signal.signal_type}",
            "TRADE",
        )

    # ------------------------------------------------------------------
    # Position monitor loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self):
        while self.state.running:
            try:
                await self.monitor_positions()
            except Exception as exc:
                logger.exception(f"Monitor loop error: {exc}")
            await asyncio.sleep(2)

    async def monitor_positions(self):
        """Fetch live positions from MT5, update P&L, apply trailing stops."""
        live_positions = await self.client.get_open_positions()
        live_tickets   = {p["ticket"] for p in live_positions}

        # Detect closed positions
        closed_tickets = [
            t for t in list(self.state.open_positions.keys())
            if t not in live_tickets
        ]
        for ticket in closed_tickets:
            await self._handle_closed_position(ticket)

        # Update live positions
        for lp in live_positions:
            ticket = lp["ticket"]
            pos = self.state.open_positions.get(ticket)
            if pos is None:
                # Position opened externally — add to state
                pos = PositionState(
                    ticket=ticket,
                    symbol=lp["symbol"],
                    direction=lp["type"],
                    lot_size=lp["volume"],
                    entry_price=lp["open_price"],
                    current_price=lp["current_price"],
                    sl_price=lp["sl"],
                    tp_price=lp["tp"],
                    open_time=lp["open_time"],
                    best_price=lp["current_price"],
                    comment=lp["comment"],
                )

            # Always update price and currency P&L — these don't need sym_info
            current = lp["current_price"]
            pos.current_price = current
            pos.pnl_currency  = lp["profit"]

            # Pip-denominated P&L and trailing stop need sym_info
            # lp["symbol"] is already canonical (no suffix) after _strip_suffix in client
            sym_info = await self.client.get_symbol_info(lp["symbol"])
            if sym_info:
                pip_sz = _pip_size(sym_info)
                if pos.direction == "LONG":
                    pos.pnl_pips = (current - pos.entry_price) / pip_sz
                else:
                    pos.pnl_pips = (pos.entry_price - current) / pip_sz
                if config.TRAILING_STOP:
                    await self._apply_trailing_stop(pos, lp, sym_info)

            await self.state.update_position(pos)

    async def _apply_trailing_stop(
        self, pos: PositionState, lp: dict, sym_info: dict
    ):
        pip_sz = _pip_size(sym_info)
        current = pos.current_price
        entry   = pos.entry_price
        sl      = pos.sl_price
        tp      = pos.tp_price
        atr     = self.state.scanner_results.get(pos.symbol)
        atr_val = atr.atr if atr else pip_sz * 15

        sl_distance = abs(entry - sl)
        rr_current  = pos.pnl_pips / (sl_distance / pip_sz) if sl_distance > 0 else 0

        new_sl = sl

        # Move to breakeven at 1:1 RR
        if not pos.breakeven_done and rr_current >= config.BREAKEVEN_AT_RR:
            if pos.direction == "LONG":
                new_sl = max(sl, entry)
            else:
                new_sl = min(sl, entry)
            pos.breakeven_done = True
            logger.info(f"Breakeven set for ticket={pos.ticket} {pos.symbol}")

        # Trail by 0.5 ATR once at 1.5:1 RR
        if rr_current >= 1.5:
            if pos.direction == "LONG":
                pos.best_price = max(pos.best_price, current)
                trail_sl = pos.best_price - atr_val * 1.0
                new_sl = max(new_sl, trail_sl)
            else:
                pos.best_price = min(pos.best_price, current)
                trail_sl = pos.best_price + atr_val * 1.0
                new_sl = min(new_sl, trail_sl)

        # Only modify if SL actually moved in profit direction
        if pos.direction == "LONG" and new_sl > sl + pip_sz:
            await self.client.modify_position(pos.ticket, round(new_sl, sym_info["digits"]), tp)
            pos.sl_price = new_sl
        elif pos.direction == "SHORT" and new_sl < sl - pip_sz:
            await self.client.modify_position(pos.ticket, round(new_sl, sym_info["digits"]), tp)
            pos.sl_price = new_sl

    async def _handle_closed_position(self, ticket: int):
        """Position no longer open in MT5 — record closure."""
        pos = self.state.open_positions.get(ticket)
        if pos is None:
            return

        # Fetch final deal info from history
        history = await self.client.get_trade_history(days=1)
        deal = next(
            (d for d in history if d.get("order") == ticket or d.get("ticket") == ticket),
            None,
        )

        exit_price   = deal["price"]   if deal else pos.current_price
        pnl_currency = deal["profit"]  if deal else pos.pnl_currency
        close_time   = deal["time"]    if deal else datetime.now(timezone.utc).isoformat()

        sym_info = await self.client.get_symbol_info(pos.symbol)
        pnl_pips = pos.pnl_pips
        if sym_info:
            pip_sz   = _pip_size(sym_info)
            if pos.direction == "LONG":
                pnl_pips = (exit_price - pos.entry_price) / pip_sz
            else:
                pnl_pips = (pos.entry_price - exit_price) / pip_sz

        close_reason = _infer_close_reason(deal, pos)

        record = TradeRecord(
            ticket=ticket,
            symbol=pos.symbol,
            direction=pos.direction,
            signal_type=pos.signal_type,
            lot_size=pos.lot_size,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            sl_price=pos.sl_price,
            tp_price=pos.tp_price,
            pnl_pips=round(pnl_pips, 1),
            pnl_currency=round(pnl_currency, 2),
            open_time=pos.open_time,
            close_time=close_time,
            status="CLOSED",
            close_reason=close_reason,
        )

        await self.state.add_trade_history(record)
        await self.state.remove_position(ticket)
        await self._db_close_trade(record)

        direction_emoji = "+" if pnl_currency >= 0 else ""
        await self.state.add_alert(
            f"TRADE CLOSED: {pos.symbol} {pos.direction} "
            f"{direction_emoji}{pnl_currency:.2f} ({pnl_pips:+.1f}p)",
            "TRADE",
        )
        logger.info(
            f"Trade closed | {pos.symbol} {pos.direction} "
            f"ticket={ticket} PnL={pnl_currency:+.2f} ({pnl_pips:+.1f}p)"
        )

    # ------------------------------------------------------------------
    # Account state loop
    # ------------------------------------------------------------------

    async def _account_loop(self):
        """Periodically refresh account info in shared state."""
        day_start = None
        while self.state.running:
            try:
                info = await self.client.get_account_info()
                if info:
                    now = datetime.now(timezone.utc)
                    if day_start is None or now.hour == 0 and now.minute < 1:
                        day_start = info["balance"]

                    drawdown_pct = 0.0
                    if day_start and day_start > 0:
                        drawdown_pct = max(
                            0.0,
                            (day_start - info["equity"]) / day_start * 100,
                        )

                    self.state.account = AccountState(
                        balance=info["balance"],
                        equity=info["equity"],
                        margin=info["margin"],
                        free_margin=info["free_margin"],
                        profit=info["profit"],
                        drawdown_pct=drawdown_pct,
                        day_start_balance=day_start or info["balance"],
                    )
                    self.state.mt5_connected = True
            except Exception as exc:
                logger.debug(f"Account loop error: {exc}")
                self.state.mt5_connected = False
            await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Manual close (API endpoint)
    # ------------------------------------------------------------------

    async def close_position(self, ticket: int) -> bool:
        success = await self.client.close_position(ticket)
        if success:
            # Monitor will detect closure on next cycle
            logger.info(f"Manual close requested for ticket={ticket}")
        return success

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def _db_insert_trade(
        self, ticket: int, signal: EntrySignal, entry: float, open_time: str
    ):
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO trades
                (ticket, symbol, direction, signal_type, lot_size,
                 entry_price, sl_price, tp_price, open_time, status, comment)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ticket, signal.symbol, signal.direction, signal.signal_type,
                    signal.lot_size, entry, signal.sl_price, signal.tp_price,
                    open_time, "OPEN", signal.comment,
                ),
            )
            await db.commit()

    async def _db_close_trade(self, record: TradeRecord):
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE trades SET
                    exit_price=?, pnl_pips=?, pnl_currency=?,
                    close_time=?, status=?, close_reason=?
                WHERE ticket=?
                """,
                (
                    record.exit_price, record.pnl_pips, record.pnl_currency,
                    record.close_time, record.status, record.close_reason,
                    record.ticket,
                ),
            )
            await db.commit()

    async def _log_scanner(self, result, action: str):
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO scanner_log
                (timestamp, symbol, pullback_score, breakout_score,
                 direction, signal_type, action)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    result.symbol,
                    result.pullback_score,
                    result.breakout_score,
                    result.direction,
                    result.signal_type,
                    action,
                ),
            )
            await db.commit()
