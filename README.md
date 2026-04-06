# Exness Forex Bot

Real-time algorithmic forex trading bot for **Exness MT5** with pullback and breakout entry strategies.
Live dashboard via FastAPI + WebSocket. Position monitoring with trailing stops.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Broker | Exness (MetaTrader 5) |
| Backend | Python 3.11+ · asyncio |
| Server | FastAPI + uvicorn + WebSocket |
| Data | pandas, numpy, MetaTrader5 |
| DB | SQLite via aiosqlite |
| Process | PM2 |
| UI | Single-file HTML/CSS/JS (Bloomberg terminal style) |

---

## Platform Note

> **MetaTrader5 Python library only works on Windows natively.**
> On Linux/Mac you must run MT5 terminal inside Wine and install the library via Wine Python,
> or use a Windows VM/VPS. The bot backend can run on Linux if MT5 terminal is accessible
> via Wine at localhost.

---

## MT5 Terminal Setup (Exness)

1. Download MetaTrader 5 from [exness.com](https://www.exness.com) → Platforms → MT5
2. Install and log in with your Exness account credentials
3. Find your exact **server name** inside MT5:
   - File → Open Account → search "Exness"
   - Demo servers: `Exness-MT5Trial` or `Exness-MT5Trial8`
   - Live servers: `Exness-MT5Real` or `Exness-MT5Real8`
   - The exact name appears in the bottom-right corner of MT5: `e.g. Exness-MT5Trial`
4. Enable **Algo Trading**:
   - Tools → Options → Expert Advisors → tick "Allow automated trading"
5. Keep MT5 terminal **running in the background** while the bot is active

---

## Installation

```bash
# Clone the repo
git clone <repo-url>
cd exness-forex-bot

# Create virtual environment (Windows)
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Edit `config.py` or create a `.env` file:

```env
EXNESS_ACCOUNT=123456789
EXNESS_PASSWORD=your_password_here
EXNESS_SERVER=Exness-MT5Trial
TRADING_MODE=demo
```

Key settings in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `RISK_PER_TRADE` | `1.0` | % of balance risked per trade |
| `MAX_OPEN_TRADES` | `4` | Maximum simultaneous positions |
| `MIN_RR_RATIO` | `2.0` | Minimum reward-to-risk ratio |
| `SCANNER_MIN_SCORE` | `65` | Minimum score to show as opportunity |
| `TRAILING_STOP` | `True` | Enable trailing stop management |
| `PRIMARY_TIMEFRAME` | `M15` | Chart timeframe for entries |
| `TREND_TIMEFRAME` | `H1` | Higher timeframe for trend filter |

---

## Running the Bot

### Direct Python

```bash
python main.py
```

### Via PM2 (recommended for production)

```bash
# Install PM2 if needed
npm install -g pm2

# Start bot
pm2 start ecosystem.config.js

# View logs
pm2 logs exness-forex-bot

# Stop bot
pm2 stop exness-forex-bot

# Restart bot
pm2 restart exness-forex-bot

# Monitor
pm2 monit
```

Open the dashboard at: **http://localhost:8080**

---

## Dashboard

The UI is a single-file Bloomberg-terminal-style dashboard at `ui/index.html`.

- **Top bar** — account balance, equity, free margin, day P&L, WS connection status
- **Scanner Feed** — live symbol scores with pullback/breakout badges, sorted by score
- **Open Positions** — live P&L cards with SL/TP progress bars and manual close button
- **Trade History** — closed trades table with win rate, total pips, total P&L
- **Alerts Ticker** — scrolling signal and trade event feed

---

## Strategies

### Pullback Strategy
Enters on retracements within a trending market.

- **Trend filter** (H1): EMA20 > EMA50 for bullish, EMA20 < EMA50 for bearish
- **Fibonacci levels**: 38.2%, 50%, 61.8% retracement from last 50-candle swing
- **RSI confirmation**: 30–45 for longs, 55–70 for shorts
- **Candlestick patterns**: hammer, engulfing, morning/evening star
- **Volume**: low volume on pullback candles (healthy), recovery on entry candle
- **Entry**: market order | **SL**: below swing low + ATR buffer | **TP**: 2:1 minimum

### Breakout Strategy
Enters on range breakouts with volume confirmation.

- **Consolidation**: last 8 candles ATR < 0.35% of price, range < 0.5%
- **Level**: resistance/support from last 30 candles with 3+ touches
- **Breakout candle**: closes beyond level with body > 60% of range
- **Volume**: breakout candle volume > 1.8× 20-period MA
- **Entry**: stop-limit order at level + 2 pips | **SL**: opposite end of range | **TP**: range × 1.5

---

## Risk Management

- Fixed fractional sizing: `lot = risk_amount / (sl_pips × pip_value_per_lot)`
- Maximum lot cap: 5.0 lots
- Breakeven: SL moves to entry at 1:1 R:R
- Trailing stop: trails by 1× ATR once position reaches 1.5:1 R:R
- Daily drawdown stop: bot pauses new entries if daily drawdown > 5%
- Session filter: London (08:00–17:00 UTC) and New York (13:00–22:00 UTC)

---

## Switching Demo ↔ Live

1. Open `config.py` (or `.env`)
2. Change `EXNESS_SERVER` to `Exness-MT5Real` (or `Exness-MT5Real8`)
3. Change `TRADING_MODE` to `"live"`
4. Make sure MT5 terminal is connected to the live account
5. Restart the bot: `pm2 restart exness-forex-bot`

> **Warning**: switching to live mode trades real money. Double-check all settings.

---

## File Structure

```
exness-forex-bot/
├── core/
│   ├── mt5_client.py      # MT5 connection, data fetch, order execution
│   ├── scanner.py         # Background symbol scanner
│   ├── strategy.py        # Pullback + Breakout logic
│   ├── order_manager.py   # Entry, SL, TP, trailing, position management
│   └── state.py           # Thread-safe shared in-memory state
├── server/
│   ├── app.py             # FastAPI server + REST endpoints
│   └── ws_handler.py      # WebSocket broadcast
├── ui/
│   └── index.html         # Single-file trading dashboard
├── config.py              # All settings and credentials
├── main.py                # Entry point
├── ecosystem.config.js    # PM2 config
├── requirements.txt
└── README.md
```

---

## Database

SQLite at `trades.db` with two tables:

- **`trades`** — all opened/closed positions with full P&L record
- **`scanner_log`** — scanner run history (score snapshots)

---

## Disclaimer

This software is for educational and research purposes. Forex trading involves significant risk of loss.
Always test on a demo account before trading live. Past performance does not guarantee future results.
