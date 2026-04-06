"""
main.py — Entry point for the Exness Forex Bot.

Starts three concurrent async tasks:
  1. Scanner     — polls symbols every SCANNER_INTERVAL seconds
  2. OrderManager — strategy evaluation + position monitoring
  3. FastAPI server — REST API + WebSocket broadcast

Usage:
    python main.py

Via PM2:
    pm2 start ecosystem.config.js
"""

import asyncio
import sys

from loguru import logger

import config
from core.mt5_client import MT5Client
from core.state import SharedState
from core.scanner import Scanner
from core.order_manager import OrderManager
from server.app import start_server


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    import os
    os.makedirs(config.LOG_DIR, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.LOG_LEVEL,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )
    logger.add(
        f"{config.LOG_DIR}/bot-out.log",
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
    )
    logger.add(
        f"{config.LOG_DIR}/bot-error.log",
        level="ERROR",
        rotation="5 MB",
        retention=5,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    setup_logging()

    logger.info("=" * 60)
    logger.info("  EXNESS FOREX BOT — Starting up")
    logger.info(f"  Mode    : {config.TRADING_MODE.upper()}")
    logger.info(f"  Server  : {config.EXNESS_SERVER}")
    logger.info(f"  Account : {config.EXNESS_ACCOUNT}")
    logger.info(f"  UI      : http://localhost:{config.SERVER_PORT}")
    logger.info("=" * 60)

    # 1. Connect to MT5
    client = MT5Client()
    try:
        await client.connect()
    except (ConnectionError, RuntimeError) as exc:
        logger.critical(f"MT5 connection failed: {exc}")
        logger.critical(
            "Make sure MT5 terminal is running and credentials are correct in config.py / .env"
        )
        sys.exit(1)

    # 2. Shared state
    state = SharedState()
    state.mt5_connected = True

    # 3. Components
    scanner       = Scanner(client, state)
    order_manager = OrderManager(client, state)

    # 4. Run everything concurrently
    try:
        await asyncio.gather(
            scanner.run(),
            order_manager.run(),
            start_server(state, order_manager),
        )
    except KeyboardInterrupt:
        logger.info("Shutdown requested.")
    finally:
        state.running = False
        await client.disconnect()
        logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
