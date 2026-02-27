# ─────────────────────────────────────────────────────────────
#  config.py  —  Gap Scanner Configuration
#  !! Never commit this file to GitHub or share it !!
# ─────────────────────────────────────────────────────────────

# Get these from: https://app.alpaca.markets → API Keys
ALPACA_API_KEY    = "PKYIEPPFCLURSTD5WDQ6WT3SOT"
ALPACA_SECRET_KEY = "8WCdJN16KZLTfVDRJVTf9dTp7VDYcMHqfDXFVo58PXve"

# PAPER = True  → paper trading account (safe, use for testing)
PAPER = True

# Your trading account size (used for position sizing)
ACCOUNT_SIZE = 92000

# Default risk per trade (% of account)
DEFAULT_RISK_PCT = 0.5

# ─────────────────────────────────────────────────────────────
#  Email Alerts  (Gmail App Password)
#
#  One-time setup:
#    1. Enable 2FA:  https://myaccount.google.com/security
#    2. App Password: https://myaccount.google.com/apppasswords
#       → Select "Mail" + "Other (Gap Scanner)" → copy 16-char key
#    3. Fill in below, set EMAIL_ENABLED = True
# ─────────────────────────────────────────────────────────────

EMAIL_ENABLED      = True                    # ← flip True after setup
EMAIL_FROM         = "wonderusky@gmail.com"          # your Gmail
EMAIL_APP_PASSWORD = "ripv mrlb zxok fmyx"    # 16-char app password (spaces ok)
EMAIL_TO           = "wonderusky@gmail.com"          # recipient (can be same address)

# ─────────────────────────────────────────────────────────────
#  Auto-Scan Scheduler
#  Scans every SCAN_INTERVAL_MIN during the window (ET time)
#  Window: 8:00 AM → 10:30 AM covers pre-market + first hour
# ─────────────────────────────────────────────────────────────

SCHEDULER_ENABLED  = True          # ← flip True to enable
SCAN_INTERVAL_MIN  = 5              # scan every N minutes
SCAN_WINDOW_START  = "08:00"        # ET start
SCAN_WINDOW_END    = "10:30"        # ET end

# What triggers an email:
ALERT_ON_GO        = True           # verdict == GO
ALERT_ON_WATCH     = True           # verdict == WATCH
ALERT_ON_NEW       = True           # ticker not seen in previous scan

# ─────────────────────────────────────────────────────────────
#  Auto-Execution  (paper account recommended)
#
#  When enabled, the scheduler will automatically place bracket
#  orders for GO setups that are above VWAP.
#
#  Safety limits:
#    AUTO_MAX_TRADES_PER_DAY  — hard stop after N fills
#    AUTO_REQUIRE_ABOVE_VWAP  — skip if price is below VWAP
#    AUTO_VERDICTS            — which verdicts trigger ("GO", "WATCH")
#
#  Set AUTO_TRADE_ENABLED = True to activate.
#  Only works when SCHEDULER_ENABLED = True.
# ─────────────────────────────────────────────────────────────

AUTO_TRADE_ENABLED       = True        # ← flip True to enable
AUTO_VERDICTS            = ["GO"]       # ["GO"] or ["GO", "WATCH"]
AUTO_MAX_TRADES_PER_DAY  = 3           # hard daily cap
AUTO_REQUIRE_ABOVE_VWAP  = True        # must be above VWAP to trade
AUTO_MIN_SCORE           = 30          # minimum scanner score to trade
AUTO_MONITOR_INTERVAL    = 60          # seconds between order status checks

# ─────────────────────────────────────────────────────────────
#  Dynamic Universe — Alpaca Screener
#  Pulls top gainers + most actives fresh each scan.
#  Your static watchlist is always merged in as a base.
# ─────────────────────────────────────────────────────────────

SCREENER_TOP_GAINERS = 50    # top N gainers from market movers API
SCREENER_TOP_ACTIVES = 50    # top N most active by volume
