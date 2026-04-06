"""
server/ws_handler.py — WebSocket connection registry and broadcast loop.
"""

import asyncio
import json
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

import config
from core.state import SharedState


class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)
        logger.debug(f"WS client connected. Total: {len(self.active)}")

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)
        logger.debug(f"WS client disconnected. Total: {len(self.active)}")

    async def broadcast(self, data: dict):
        if not self.active:
            return
        payload = json.dumps(data, default=str)
        dead = set()
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


async def websocket_endpoint(websocket: WebSocket, state: SharedState):
    """Handle a single WebSocket client connection."""
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive by waiting for any client message
            # (ping frames are handled by uvicorn automatically)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


async def broadcast_loop(state: SharedState):
    """Continuously broadcast the state snapshot to all connected clients."""
    logger.info("WebSocket broadcast loop started.")
    while state.running:
        try:
            if manager.active:
                snapshot = state.get_snapshot()
                await manager.broadcast(snapshot)
        except Exception as exc:
            logger.debug(f"Broadcast error: {exc}")
        await asyncio.sleep(config.UI_BROADCAST_INTERVAL)
