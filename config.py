"""
config.py — All settings, credentials, and mode toggle for Exness Forex Bot.
Edit this file before running. Never commit real credentials to version control.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# MT5 / Exness credentials
# ---------------------------------------------------------------------------
EXNESS_ACCOUNT = int(os.getenv("EXNESS_ACCOUNT", "123456789"))
EXNESS_PASSWORD = os.getenv("EXNESS_PASSWORD", "")
EXNESS_SERVER = os.getenv("EXNESS_SERVER", "Exness-MT5Trial")

# "demo" or "live" — used as an informational label in the UI
TRADING_MODE = os.getenv("TRADING_MODE", "demo")

# Symbol suffix used by this broker (auto-detected on connect).
# Override in .env only if auto-detection fails, e.g. SYMBOL_SUFFIX=m
# Leave empty for auto-detection.
SYMBOL_SUFFIX = os.getenv("SYMBOL_SUFFIX", "")

# ---------------------------------------------------------------------------
# Symbols to scan (Exness forex + metals + crypto CFDs)
# ---------------------------------------------------------------------------
SYMBOLS_WATCHLIST = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "NZDUSD", "USDCAD",
    "GBPJPY", "EURJPY", "EURGBP",
    "XAUUSD",   # Gold
    "XAGUSD",   # Silver
    "BTCUSD",   # Crypto CFD
    "ETHUSD",
]

# ---------------------------------------------------------------------------
# Scanner settings
# ---------------------------------------------------------------------------
SCANNER_INTERVAL = 20        # seconds between scanner runs
SCANNER_MIN_SCORE = 65       # minimum score to mark as opportunity

# ---------------------------------------------------------------------------
# Strategy / indicator settings
# ---------------------------------------------------------------------------
PRIMARY_TIMEFRAME = "M15"
TREND_TIMEFRAME = "H1"
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

# Per-symbol max spread in pips — symbols not listed use DEFAULT_MAX_SPREAD
SYMBOL_MAX_SPREAD = {
    "XAUUSD": 5.0,
    "BTCUSD": 50.0,
    "ETHUSD": 5.0,
}
DEFAULT_MAX_SPREAD = 3.0     # pips

# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------
RISK_PER_TRADE = 1.0         # % of account balance per trade
MAX_OPEN_TRADES = 4
MAX_TRADES_PER_SYMBOL = 1
TRAILING_STOP = True
BREAKEVEN_AT_RR = 1.0        # move SL to breakeven at 1:1 R:R
MIN_RR_RATIO = 2.0           # minimum reward-to-risk required to take a trade
MAX_LOT_SIZE = 5.0           # hard cap per trade
MAX_DAILY_DRAWDOWN_PCT = 5.0 # stop trading if daily drawdown exceeds this %

# SL buffer multiplier (applied to ATR beyond swing high/low)
SL_ATR_BUFFER = 0.3

# Maximum SL distance as a multiple of ATR (caps runaway SL on volatile pairs)
SL_ATR_CAP = 2.0

# Minimum SL distance in pips (symbol-independent floor)
MIN_SL_PIPS = 10.0

# Breakout entry — stop-limit offset beyond breakout level (pips)
BREAKOUT_ENTRY_OFFSET_PIPS = 2.0

# ---------------------------------------------------------------------------
# Session filter (UTC hours)
# ---------------------------------------------------------------------------
LONDON_OPEN_HOUR = 8
LONDON_CLOSE_HOUR = 17
NY_OPEN_HOUR = 13
NY_CLOSE_HOUR = 22

# Symbols allowed during Asian / off-hours session
ASIAN_SESSION_SYMBOLS = {"USDJPY", "AUDUSD", "NZDUSD", "XAUUSD"}

# ---------------------------------------------------------------------------
# Server / API
# ---------------------------------------------------------------------------
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8080
UI_BROADCAST_INTERVAL = 0.5  # seconds between WebSocket broadcasts

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = "trades.db"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = "INFO"
LOG_DIR = "logs"

# ---------------------------------------------------------------------------
# Runtime config file (non-credential settings, editable via UI)
# ---------------------------------------------------------------------------
CONFIG_JSON_PATH = "bot_config.json"

# Apply any previously-saved runtime config overrides
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
