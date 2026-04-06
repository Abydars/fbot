"""
server/app.py — FastAPI application: REST endpoints + WebSocket + static UI.
"""

import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import uvicorn

import config
from core.state import SharedState
from server.ws_handler import websocket_endpoint, broadcast_loop, manager


def create_app(state: SharedState) -> FastAPI:
    app = FastAPI(title="Exness Forex Bot", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Lazy import to avoid circular dependency at module load time
    _order_manager_ref: list = []

    def _get_om():
        if _order_manager_ref:
            return _order_manager_ref[0]
        return None

    def set_order_manager(om):
        _order_manager_ref.clear()
        _order_manager_ref.append(om)

    app.set_order_manager = set_order_manager  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Static UI
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def serve_ui():
        ui_path = Path(__file__).parent.parent / "ui" / "index.html"
        if not ui_path.exists():
            return HTMLResponse("<h1>UI not found</h1>", status_code=404)
        return HTMLResponse(ui_path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status():
        return JSONResponse({
            "running":       state.running,
            "paused":        state.paused,
            "mt5_connected": state.mt5_connected,
            "trading_mode":  config.TRADING_MODE,
            "trades_today":  state.trades_today,
            "win_rate":      state.win_rate(),
            "open_count":    state.open_position_count(),
        })

    @app.get("/api/account")
    async def api_account():
        return JSONResponse(state.account.to_dict())

    @app.get("/api/positions")
    async def api_positions():
        return JSONResponse([p.to_dict() for p in state.open_positions.values()])

    @app.get("/api/history")
    async def api_history():
        return JSONResponse([t.to_dict() for t in state.trade_history[-100:]])

    @app.get("/api/ohlcv/{symbol}")
    async def api_ohlcv(symbol: str, tf: str = "M15", count: int = 200):
        """Fetch OHLCV candles for the chart. Used by the UI chart panel."""
        from core.mt5_client import MT5Client as _MT5
        # Grab the client from order manager if available
        om = _get_om()
        client = getattr(om, "client", None) if om else None
        if client is None:
            raise HTTPException(status_code=503, detail="MT5 client not ready")
        df = await client.get_ohlcv(symbol, tf, count)
        if df is None:
            raise HTTPException(status_code=404, detail=f"No data for {symbol} {tf}")
        # Return as list of {time, open, high, low, close, volume}
        df = df.reset_index()
        df["time"] = df["time"].astype("int64") // 10**9   # Unix seconds
        return JSONResponse(df[["time","open","high","low","close","volume"]].to_dict("records"))

    @app.post("/api/close/{ticket}")
    async def api_close(ticket: int):
        om = _get_om()
        if om is None:
            raise HTTPException(status_code=503, detail="Order manager not ready")
        success = await om.close_position(ticket)
        if not success:
            raise HTTPException(status_code=400, detail=f"Failed to close ticket {ticket}")
        return JSONResponse({"status": "ok", "ticket": ticket})

    @app.post("/api/pause")
    async def api_pause():
        state.paused = True
        await state.add_alert("Bot paused — no new entries.", "WARN")
        logger.warning("Bot paused via API.")
        return JSONResponse({"status": "paused"})

    @app.post("/api/resume")
    async def api_resume():
        state.paused = False
        await state.add_alert("Bot resumed.", "INFO")
        logger.info("Bot resumed via API.")
        return JSONResponse({"status": "running"})

    # ------------------------------------------------------------------
    # WebSocket endpoint
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket):
        await websocket_endpoint(websocket, state)

    return app


async def start_server(state: SharedState, order_manager=None):
    """Start FastAPI + broadcast loop. Intended to run inside asyncio.gather()."""
    app = create_app(state)
    if order_manager is not None:
        app.set_order_manager(order_manager)  # type: ignore[attr-defined]

    config_uvicorn = uvicorn.Config(
        app,
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config_uvicorn)

    logger.info(f"HTTP server starting on http://{config.SERVER_HOST}:{config.SERVER_PORT}")

    await asyncio.gather(
        server.serve(),
        broadcast_loop(state),
    )
