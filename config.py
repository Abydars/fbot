"""
config.py — Bot settings.

Credentials come from .env (never commit real credentials).
Risk/runtime settings are persisted to bot_config.json and editable via the UI.
Everything else is an internal constant the bot manages itself.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# MT5 / Exness credentials  (set in .env)
# ---------------------------------------------------------------------------
EXNESS_ACCOUNT  = int(os.getenv("EXNESS_ACCOUNT", "123456789"))
EXNESS_PASSWORD = os.getenv("EXNESS_PASSWORD", "")
EXNESS_SERVER   = os.getenv("EXNESS_SERVER", "Exness-MT5Trial")
TRADING_MODE    = os.getenv("TRADING_MODE", "demo")

# Symbol suffix used by this broker (auto-detected on connect).
# Override in .env only if auto-detection fails, e.g. SYMBOL_SUFFIX=m
SYMBOL_SUFFIX   = os.getenv("SYMBOL_SUFFIX", "")

# ---------------------------------------------------------------------------
# Risk management  (editable via UI → saved to bot_config.json)
# ---------------------------------------------------------------------------
RISK_PER_TRADE         = 1.0   # % of balance risked per trade
MAX_OPEN_TRADES        = 4     # max concurrent positions
MAX_LOT_SIZE           = 5.0   # hard lot cap per trade
MAX_DAILY_DRAWDOWN_PCT = 5.0   # pause trading if daily drawdown exceeds this %
TRAILING_STOP          = True  # enable trailing stop
BREAKEVEN_AT_RR        = 1.0   # move SL to breakeven once R:R reaches this value

# ---------------------------------------------------------------------------
# Internal constants  (not user-configurable)
# ---------------------------------------------------------------------------
SCANNER_INTERVAL          = 20      # seconds between full scan cycles
MIN_SL_PIPS               = 10.0   # minimum SL distance (sanity floor)
BREAKOUT_ENTRY_OFFSET_PIPS = 2.0   # pips beyond breakout level for stop-limit entry

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST            = "0.0.0.0"
SERVER_PORT            = 8080
UI_BROADCAST_INTERVAL  = 0.5   # seconds between WebSocket broadcasts

# ---------------------------------------------------------------------------
# Database / logging
# ---------------------------------------------------------------------------
DB_PATH          = "trades.db"
CONFIG_JSON_PATH = "bot_config.json"
LOG_LEVEL        = "INFO"
LOG_DIR          = "logs"

# ---------------------------------------------------------------------------
# Runtime config overrides from bot_config.json
# ---------------------------------------------------------------------------
import json as _json
from pathlib import Path as _Path
_config_json = _Path(__file__).parent / CONFIG_JSON_PATH
if _config_json.exists():
    try:
        for _k, _v in _json.loads(_config_json.read_text()).items():
            if _k in globals():
                globals()[_k] = _v
    except Exception:
        pass
