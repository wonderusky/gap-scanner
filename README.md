# Gap Scanner — Live Trading System

> Semi-automated pre-market gap scanner with bracket order execution, live P&L tracking, and manual ticker lookup with full technicals.

---

## Project Structure

```
trading/
├── server.py       ← Flask backend (Alpaca API + yfinance)
├── scanner.html    ← Web UI (open in browser)
├── config.py       ← API keys and account settings (never share)
├── start.sh        ← One-command launcher
├── requirements.txt
└── README.md
```

---

## Quick Start

### First Time Setup

```bash
cd ~/Code/trading

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Add Your Alpaca Keys

Edit `config.py`:

```python
ALPACA_API_KEY    = "PK..."   # Paper key starts with PK
ALPACA_SECRET_KEY = "..."
PAPER             = True       # True = paper trading, False = live
ACCOUNT_SIZE      = 100000     # Used for position sizing
DEFAULT_RISK_PCT  = 0.5        # 0.5% risk per trade
```

Get keys from:
- Paper: https://app.alpaca.markets/paper/dashboard/overview
- Live: https://app.alpaca.markets/brokerage/dashboard/overview

> ⚠️ Never commit `config.py` to GitHub. It's in `.gitignore` already.

### Start the Server

```bash
cd ~/Code/trading
source venv/bin/activate
python server.py
```

Or use the one-liner (after `chmod +x start.sh` the first time):

```bash
./start.sh
```

Expected startup banner:
```
════════════════════════════════════════════════════
  GAP SCANNER — Alpaca + yfinance
════════════════════════════════════════════════════
  Alpaca:    ✅
  yfinance:  ✅ v1.x.x
  API Keys:  ✅
  Trading:   ✅ PAPER
  Universe:  85 tickers
  Server:    http://localhost:5001
════════════════════════════════════════════════════
```

### Open the Scanner

Open `scanner.html` in Chrome. The **ALPACA LIVE** badge in the top right should turn green.

> Best time to scan: **6:00 – 9:25 AM CT** (pre-market window)

---

## Features

### Gap Scanner

Scans 85+ tickers every run and filters by:

| Filter | Default | Notes |
|---|---|---|
| Min Gap % | 3% | Minimum overnight gap |
| Max Gap % | 30% | Avoids parabolic traps |
| Min Pre-Market Vol | 500k | Confirms institutional interest |
| Min Price | $5 | Avoids penny stocks |
| Max Price | $200 | Position size stays manageable |

Each result shows:

- **RANK** — A/B/C/SKIP verdict based on 4-factor scoring
- **GAP %** — overnight gap from previous close
- **PRICE** — live Alpaca quote (bid/ask midpoint)
- **PM VOL** — pre-market volume with visual bar
- **REL VOL** — volume vs 30-day average
- **VWAP** — price vs VWAP with % difference and direction
- **CATALYST** — NEWS badge if SEC 8-K filing detected
- **SCORE** — 0–100 composite score
- **SHARES / EXPOSURE** — position size at current risk settings
- **STOP** — calculated stop loss price
- **TARGET +1%** — first take-profit level
- **FINVIZ** — direct chart link
- **TRADE** — BUY button to open order modal

### Manual Ticker Lookup

Type any ticker in the **search box in the header** (top right) and press Enter.

Shows a full dropdown with:
- Live quote: bid, ask, open, high, low, prev close
- Day volume and relative volume
- 52-week high/low
- VWAP with % above/below
- EMA 8, EMA 21, SMA 50, SMA 200 — each with ▲/▼ and % distance
- RSI (14) with zone label and visual bar
- Fundamentals: market cap, float, short %, P/E
- **BUY / SELL / CHART** action buttons

### Trading

#### BUY — Bracket Order

Click **BUY** on any scanner row or from the ticker search dropdown.

The modal pre-fills:
- **Shares** — calculated from account size × risk % ÷ stop distance
- **Stop Loss** — calculated from scanner or 2% below price
- **Take Profit** — +1% from entry (adjustable)
- **Limit Price** — leave blank for market order, enter price for limit

Bracket order = entry + automatic stop-loss leg + automatic take-profit leg submitted as one order. When one leg fills, the other cancels automatically.

> For market orders, stop is auto-adjusted 2% below the quoted price to account for fill price differences.

#### SELL — Market Order

Click **SELL** in the positions panel to close any open position at market immediately.

#### Positions Panel

Scroll to the bottom of the page. Click the **POSITIONS** bar to expand.

Shows:
- All open positions with entry price, current price, market value
- Unrealized P&L in dollars and percent (green/red)
- Account equity, buying power, daytrade count
- PAPER / LIVE badge
- **⚠ FLATTEN ALL** button — closes every position and cancels all orders immediately

Auto-refreshes every 5 seconds when expanded.

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/scan` | POST | Run scan with filter params |
| `/api/results` | GET | Get current scan results |
| `/api/status` | GET | Server health + key status |
| `/api/quote/<ticker>` | GET | Full quote + technicals for any ticker |
| `/api/account` | GET | Account equity, buying power, DT count |
| `/api/positions` | GET | All open positions with live P&L |
| `/api/orders` | GET | Open/pending orders |
| `/api/order` | POST | Place buy or sell order |
| `/api/order/<id>` | DELETE | Cancel a specific order |
| `/api/position/<ticker>` | DELETE | Flatten a specific position |
| `/api/closeall` | POST | Emergency: close all positions + cancel all orders |
| `/api/diagnostic` | GET | Debug info |

---

## Scoring System

### Verdict Ranks

| Rank | Criteria | Action |
|---|---|---|
| **A** | Passes all 4 checks | Trade if VWAP/EMA confirms |
| **B** | Passes 3 of 4 | Trade with extra caution |
| **C** | Passes 2 of 4 | Skip — high fakeout risk |
| **SKIP** | Fails 3+ or gap >25% | Do not trade |

### The 4 Checks

1. Relative Volume ≥ 2× average
2. Catalyst detected (NEWS / earnings)
3. Float < 100M shares
4. Gap between 4% and 20%

### Scoring Weights

| Factor | Weight | Rationale |
|---|---|---|
| Relative Volume | 35% | Institutional confirmation |
| Catalyst Quality | 30% | News quality drives follow-through |
| Float Size | 20% | Small float = faster moves |
| Gap % | 15% | Sweet spot 4–20% |

---

## Strategy Rules

| Rule | Value |
|---|---|
| Risk per trade | 0.5% of account ($460 on $92k) |
| Max exposure per position | 25% of account |
| Trading window | First 30 minutes (9:30–10:00 AM CT) |
| Min gap for consideration | 3% |
| Stop placement | Below pre-market low or VWAP |
| Target | VWAP +1% minimum |

---

## Troubleshooting

**401 Unauthorized on scan**
- Keys expired or regenerated — update `config.py` and restart server
- Paper keys start with `PK`, live keys start with `AK`
- `PAPER = True` must match key type

**Server shows old routes (grep -c "@app.route" server.py < 16)**
- Old file is running — replace with latest `server.py` download and restart

**Stop loss rejected by Alpaca (base_price error)**
- Scanner auto-adjusts stop 2% below quoted price for market orders
- If manually entering stop, make sure it's below current price

**Technicals not loading in ticker search**
- yfinance pulls 1 year of daily data — takes 2–4 seconds
- Normal during off-hours; during market hours data is fresher

**P&L not updating**
- Click the POSITIONS bar at bottom to expand (collapsed by default)
- Panel auto-refreshes every 5 seconds when open

**404 on /api/positions or /api/account**
- Running old server.py without trading endpoints
- Replace file and restart

---

## Security

- Never share or commit `config.py`
- Never paste API keys in chat, email, or Slack
- Rotate keys immediately at: https://app.alpaca.markets → API Keys → Regenerate
- Paper keys give full real market data with zero risk — use for all testing

---

## Disclaimer

This is a personal data and analysis tool. All trade decisions and execution are your own responsibility. Past performance of any setup does not guarantee future results. Always paper trade first to validate your edge.
