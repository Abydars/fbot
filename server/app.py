"""
server/app.py — FastAPI application: REST endpoints + WebSocket + static UI.
"""

import asyncio
import importlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger
import uvicorn

import config
from core.state import SharedState
from server.ws_handler import websocket_endpoint, broadcast_loop, manager

# ---------------------------------------------------------------------------
# Config schema — all keys exposed to the UI
# ---------------------------------------------------------------------------

CONFIG_SCHEMA = {
    "connection": {
        "label": "MT5 Connection",
        "fields": {
            "EXNESS_ACCOUNT":  {"type": "number",   "label": "Account Number"},
            "EXNESS_PASSWORD": {"type": "password", "label": "Password"},
            "EXNESS_SERVER":   {"type": "text",     "label": "Server Name"},
            "TRADING_MODE":    {"type": "select",   "label": "Mode", "options": ["demo", "live"]},
            "SYMBOL_SUFFIX":   {"type": "text",     "label": "Symbol Suffix (leave blank for auto-detect)"},
        }
    },
    "risk": {
        "label": "Risk Management",
        "fields": {
            "RISK_PER_TRADE":          {"type": "number", "label": "Risk Per Trade (%)",     "min": 0.1, "max": 10,  "step": 0.1},
            "MAX_OPEN_TRADES":         {"type": "number", "label": "Max Concurrent Trades",  "min": 1,   "max": 20},
            "MAX_LOT_SIZE":            {"type": "number", "label": "Max Lot Size",           "min": 0.01,"max": 100, "step": 0.01},
            "MAX_DAILY_DRAWDOWN_PCT":  {"type": "number", "label": "Max Daily Drawdown (%)", "min": 0.5, "max": 50,  "step": 0.5},
            "TRAILING_STOP":           {"type": "bool",   "label": "Trailing Stop"},
            "BREAKEVEN_AT_RR":         {"type": "number", "label": "Move to Breakeven at R:R","min": 0.5, "max": 5,   "step": 0.5},
        }
    },
}


def _read_config_values() -> dict:
    """Return current live values for all schema keys."""
    values = {}
    for section in CONFIG_SCHEMA.values():
        for key in section["fields"]:
            val = getattr(config, key, None)
            if isinstance(val, list):
                val = "\n".join(val)
            values[key] = val
    return values


def _write_json_config(updates: dict[str, Any]) -> None:
    """Persist runtime config to bot_config.json (never touches .env)."""
    import json
    json_path = Path(__file__).parent.parent / config.CONFIG_JSON_PATH
    existing: dict = {}
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Coerce to the same types that _apply_config produces before saving
    for key, val in updates.items():
        field_meta = None
        for section in CONFIG_SCHEMA.values():
            if key in section["fields"]:
                field_meta = section["fields"][key]
                break
        if field_meta is None:
            continue
        ftype = field_meta["type"]
        if ftype == "number":
            orig = getattr(config, key, 0)
            val = int(val) if isinstance(orig, int) else float(val)
        elif ftype == "bool":
            val = str(val).lower() in ("true", "1", "yes")
        elif ftype == "tags":
            val = [s.strip() for s in str(val).splitlines() if s.strip()]
        else:
            val = str(val)
        existing[key] = val

    json_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _apply_config(updates: dict[str, Any]) -> None:
    """Apply updates to the live config module without restarting."""
    for key, val in updates.items():
        field_meta = None
        for section in CONFIG_SCHEMA.values():
            if key in section["fields"]:
                field_meta = section["fields"][key]
                break

        if field_meta is None:
            continue

        ftype = field_meta["type"]
        if ftype == "number":
            # Preserve int vs float
            orig = getattr(config, key, 0)
            val = int(val) if isinstance(orig, int) else float(val)
        elif ftype == "bool":
            val = str(val).lower() in ("true", "1", "yes")
        elif ftype == "tags":
            val = [s.strip() for s in str(val).splitlines() if s.strip()]
        else:
            val = str(val)

        setattr(config, key, val)
        logger.info(f"Config updated: {key} = {val!r}")


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
        # Use .timestamp() — the only reliable way with tz-aware DatetimeTZDtype.
        # .astype("int64") returns nanoseconds but silently gives wrong results
        # for tz-aware columns, producing timestamps near Unix epoch (Jan 1970).
        df["time"] = df["time"].apply(lambda x: int(x.timestamp()))
        records = df[["time","open","high","low","close","volume"]].to_dict("records")
        return JSONResponse(records)

    @app.post("/api/close/{ticket}")
    async def api_close(ticket: int):
        om = _get_om()
        if om is None:
            raise HTTPException(status_code=503, detail="Order manager not ready")
        success = await om.close_position(ticket)
        if not success:
            raise HTTPException(status_code=400, detail=f"Failed to close ticket {ticket}")
        return JSONResponse({"status": "ok", "ticket": ticket})

    @app.post("/api/close_all")
    async def api_close_all():
        om = _get_om()
        if om is None:
            raise HTTPException(status_code=503, detail="Order manager not ready")
        tickets = list(state.open_positions.keys())
        results = {"closed": [], "failed": []}
        for ticket in tickets:
            ok = await om.close_position(ticket)
            (results["closed"] if ok else results["failed"]).append(ticket)
        return JSONResponse(results)

    @app.get("/api/config")
    async def api_config_get():
        return JSONResponse({"schema": CONFIG_SCHEMA, "values": _read_config_values()})

    @app.post("/api/config")
    async def api_config_post(body: dict):
        try:
            _apply_config(body)
            _write_json_config(body)
            await state.add_alert("Config updated and saved.", "INFO")
            return JSONResponse({"status": "ok", "updated": list(body.keys())})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

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
