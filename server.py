"""
Gap Scanner Backend — Alpaca + yfinance
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Data sources:
  Alpaca   → real-time pre-market price, volume, gap %
  yfinance → float, sector, avg volume, earnings dates

Setup (one time):
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt

Run every morning:
  ./start.sh
"""

import os
import re
import threading
import concurrent.futures
import xml.etree.ElementTree as ET
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime import datetime, date, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# ── Alpaca data ───────────────────────────────────────────────────────────────
try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests   import StockSnapshotRequest
    ALPACA_OK = True
except ImportError:
    ALPACA_OK = False
    print("⚠  alpaca-py missing — run: pip install -r requirements.txt")

# ── Alpaca trading ─────────────────────────────────────────────────────────────
try:
    from alpaca.trading.client   import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest, LimitOrderRequest,
        GetOrdersRequest, ClosePositionRequest,
    )
    from alpaca.trading.enums import (
        OrderSide, TimeInForce, OrderClass, OrderType,
        QueryOrderStatus,
    )
    TRADING_OK = True
except ImportError:
    TRADING_OK = False
    print("⚠  alpaca-py trading missing")

# ── yfinance ──────────────────────────────────────────────────────────────────
try:
    import yfinance as yf
    YF_OK = True
except ImportError:
    YF_OK = False
    print("⚠  yfinance missing — run: pip install -r requirements.txt")

# ── Config ────────────────────────────────────────────────────────────────────
try:
    import config
    API_KEY    = config.ALPACA_API_KEY
    API_SECRET = config.ALPACA_SECRET_KEY
    IS_PAPER   = getattr(config, "PAPER", True)
    # Email
    EMAIL_ENABLED      = getattr(config, "EMAIL_ENABLED",      False)
    EMAIL_FROM         = getattr(config, "EMAIL_FROM",         "")
    EMAIL_APP_PASSWORD = getattr(config, "EMAIL_APP_PASSWORD", "")
    EMAIL_TO           = getattr(config, "EMAIL_TO",           "")
    # Scheduler
    SCHEDULER_ENABLED  = getattr(config, "SCHEDULER_ENABLED",  False)
    SCAN_INTERVAL_MIN  = getattr(config, "SCAN_INTERVAL_MIN",  5)
    SCAN_WINDOW_START  = getattr(config, "SCAN_WINDOW_START",  "08:00")
    SCAN_WINDOW_END    = getattr(config, "SCAN_WINDOW_END",    "10:30")
    ALERT_ON_GO        = getattr(config, "ALERT_ON_GO",        True)
    ALERT_ON_WATCH     = getattr(config, "ALERT_ON_WATCH",     True)
    ALERT_ON_NEW       = getattr(config, "ALERT_ON_NEW",       True)
    # Auto-execution
    AUTO_TRADE_ENABLED      = getattr(config, "AUTO_TRADE_ENABLED",      False)
    AUTO_VERDICTS           = getattr(config, "AUTO_VERDICTS",           ["GO"])
    AUTO_MAX_TRADES_PER_DAY = getattr(config, "AUTO_MAX_TRADES_PER_DAY", 3)
    AUTO_REQUIRE_ABOVE_VWAP = getattr(config, "AUTO_REQUIRE_ABOVE_VWAP", True)
    AUTO_MIN_SCORE          = getattr(config, "AUTO_MIN_SCORE",          30)
    AUTO_MONITOR_INTERVAL   = getattr(config, "AUTO_MONITOR_INTERVAL",   60)
    AUTO_DAILY_LOSS_LIMIT   = getattr(config, "AUTO_DAILY_LOSS_LIMIT",   -500)
    AUTO_DAILY_PROFIT_TARGET= getattr(config, "AUTO_DAILY_PROFIT_TARGET", 1000)
except ImportError:
    API_KEY    = os.environ.get("ALPACA_API_KEY", "")
    API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
    IS_PAPER   = True
    EMAIL_ENABLED     = False
    SCHEDULER_ENABLED = False
    SCAN_INTERVAL_MIN = 5
    SCAN_WINDOW_START = "08:00"
    SCAN_WINDOW_END   = "10:30"
    ALERT_ON_GO = ALERT_ON_WATCH = ALERT_ON_NEW = True
    EMAIL_FROM = EMAIL_APP_PASSWORD = EMAIL_TO = ""
    AUTO_TRADE_ENABLED      = False
    AUTO_VERDICTS           = ["GO"]
    AUTO_MAX_TRADES_PER_DAY = 3
    AUTO_REQUIRE_ABOVE_VWAP = True
    AUTO_MIN_SCORE          = 30
    AUTO_MONITOR_INTERVAL   = 60
    AUTO_DAILY_LOSS_LIMIT   = -500
    AUTO_DAILY_PROFIT_TARGET= 1000

def _tc():
    """Return a cached TradingClient (paper or live per config)."""
    return TradingClient(API_KEY, API_SECRET, paper=IS_PAPER)

app = Flask(__name__)
CORS(app)

# ── Result cache ──────────────────────────────────────────────────────────────
_cache = {
    "last_updated": None,
    "results":      [],
    "status":       "idle",   # idle | scanning | done | error
    "error":        None,
    "meta":         {"alpaca_count": 0, "yf_enriched": 0, "earnings_today": [], "skipped": {}},
}
_cache_lock = threading.Lock()

# ── yfinance fundamental cache (refreshed once per trading day) ───────────────
_yf_cache      = {}
_yf_cache_date = None
_yf_lock       = threading.Lock()

# ── Universe ──────────────────────────────────────────────────────────────────
# Static fallback list — used only if dynamic screener fails
WATCHLIST_FALLBACK = [
    "NVDA","AMD","TSLA","META","AMZN","GOOGL","MSFT","AAPL","NFLX","CRM","ORCL","ADBE",
    "SMCI","HIMS","APP","PLTR","HOOD","SOFI","UPST","MSTR","IONQ","RKLB","ACHR","JOBY",
    "AXSM","WGS","TNDM","OCGN","RXST","BBIO","AGIO","NVAX","SAVA","ACAD","SRPT",
    "RARE","EDIT","NTLA","BEAM","CRSP","FOLD","CELC","IMVT","LEGN","KYMR",
    "LUNR","LAZR","ARRY","SOUN","MARA","RIOT","CLSK","HUT","CIFR","BTBT","WULF","IREN",
    "CRDO","WOLF","CRUS","MRVL","AEHR","COHU","AMBA","LSCC","RMBS",
    "DASH","LYFT","UBER","ABNB","DKNG","PENN","AFRM","CHWY","ETSY","PINS","OPEN",
    "CHPT","BLNK","EVGO","RIVN","LCID","NIO","XPEV","LI",
]

# Keep WATCHLIST pointing at fallback for any legacy references
WATCHLIST = WATCHLIST_FALLBACK

# Screener config — tune in config.py
try:
    import config as _cfg_ref
    SCREENER_TOP_GAINERS = getattr(_cfg_ref, "SCREENER_TOP_GAINERS", 50)
    SCREENER_TOP_ACTIVES = getattr(_cfg_ref, "SCREENER_TOP_ACTIVES", 50)
    SCREENER_MIN_PRICE   = getattr(_cfg_ref, "SCREENER_MIN_PRICE",   2.0)
    SCREENER_MAX_PRICE   = getattr(_cfg_ref, "SCREENER_MAX_PRICE",   500.0)
    SCREENER_MIN_GAP     = getattr(_cfg_ref, "SCREENER_MIN_GAP",     2.0)
except ImportError:
    SCREENER_TOP_GAINERS = 50
    SCREENER_TOP_ACTIVES = 50
    SCREENER_MIN_PRICE   = 2.0
    SCREENER_MAX_PRICE   = 500.0
    SCREENER_MIN_GAP     = 2.0

# Cache the dynamic universe for the current scan to avoid re-fetching mid-scan
_universe_cache       = {"tickers": [], "fetched_at": None, "source": "fallback"}
_universe_lock        = threading.Lock()


def _fetch_dynamic_universe():
    """
    Pull top gainers + most actives from Alpaca Screener API.
    Returns (ticker_list, source_description).
    Falls back to WATCHLIST_FALLBACK on any error.
    """
    try:
        from alpaca.data import ScreenerClient, MostActivesRequest, MarketMoversRequest, MostActivesBy, MarketType
    except ImportError:
        print("  ⚠  ScreenerClient not available — using fallback watchlist")
        return WATCHLIST_FALLBACK, "fallback (screener not installed)"

    tickers = set()
    sources = []

    try:
        sc = ScreenerClient(API_KEY, API_SECRET)

        # ── Top gainers (market movers) ────────────────────────────────────────
        try:
            movers = sc.get_market_movers(
                MarketMoversRequest(top=SCREENER_TOP_GAINERS, market_type=MarketType.STOCKS)
            )
            gainers = getattr(movers, "gainers", []) or []
            for g in gainers:
                sym = getattr(g, "symbol", None) or getattr(g, "ticker", None)
                if sym:
                    tickers.add(sym.upper())
            sources.append(f"{len(gainers)} gainers")
            print(f"  🌐 Screener: {len(gainers)} top gainers")
        except Exception as e:
            print(f"  ⚠  Screener gainers error: {e}")

        # ── Most actives by volume ─────────────────────────────────────────────
        try:
            actives = sc.get_most_actives(
                MostActivesRequest(top=SCREENER_TOP_ACTIVES, by=MostActivesBy.VOLUME)
            )
            active_list = getattr(actives, "most_actives", []) or []
            added = 0
            for a in active_list:
                sym = getattr(a, "symbol", None) or getattr(a, "ticker", None)
                if sym:
                    tickers.add(sym.upper())
                    added += 1
            sources.append(f"{added} most-active")
            print(f"  🌐 Screener: {added} most actives")
        except Exception as e:
            print(f"  ⚠  Screener actives error: {e}")

    except Exception as e:
        print(f"  ⚠  ScreenerClient init error: {e}")

    if not tickers:
        print(f"  ⚠  Dynamic universe empty — falling back to watchlist ({len(WATCHLIST_FALLBACK)} tickers)")
        return WATCHLIST_FALLBACK, "fallback (screener returned empty)"

    # Filter out known bad tickers (warrants, units, preferred shares, ETFs)
    clean = [t for t in tickers if (
        len(t) <= 5 and
        not t.endswith(("W", "U", "R", "P")) and
        "." not in t and "/" not in t
    )]

    # Always include fallback list (your hand-curated names stay in)
    merged = list(set(clean) | set(WATCHLIST_FALLBACK))

    source_str = f"dynamic: {' + '.join(sources)} + {len(WATCHLIST_FALLBACK)} watchlist = {len(merged)} total"
    print(f"  ✅ Universe: {len(merged)} tickers  ({source_str})")
    return merged, source_str


def get_universe(force_refresh=False):
    """
    Return the current scan universe.
    Refreshes once per scan (cached for 4 minutes to avoid duplicate fetches).
    """
    with _universe_lock:
        now = datetime.now(timezone.utc)
        age = (now - _universe_cache["fetched_at"]).total_seconds() if _universe_cache["fetched_at"] else 999
        if not force_refresh and age < 240 and _universe_cache["tickers"]:
            return _universe_cache["tickers"], _universe_cache["source"]

    tickers, source = _fetch_dynamic_universe()

    with _universe_lock:
        _universe_cache["tickers"]    = tickers
        _universe_cache["fetched_at"] = datetime.now(timezone.utc)
        _universe_cache["source"]     = source

    return tickers, source



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Catalyst Detection — SEC EDGAR 8-K RSS Feed (free, no API key)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Keyword → catalyst type mapping (checked against 8-K title + items)
_CATALYST_KEYWORDS = {
    "earnings": [
        "earnings", "quarterly results", "q1 results", "q2 results",
        "q3 results", "q4 results", "annual results", "financial results",
        "revenue", "net income", "eps", "beats", "misses", "guidance",
    ],
    "fda": [
        "fda", "food and drug", "pdufa", "nda", "bla", "anda",
        "approval", "approved", "approvable", "complete response letter",
        "crl", "clinical trial", "phase 3", "phase 2", "trial results",
        "efficacy", "safety data", "breakthrough therapy",
    ],
    "contract": [
        "contract", "agreement", "partnership", "collaboration",
        "awarded", "selected", "deal", "supply agreement", "license",
        "joint venture", "strategic alliance",
    ],
    "upgrade": [
        "upgrade", "raised", "overweight", "outperform", "buy rating",
        "price target increased", "initiates coverage",
    ],
    "acquisition": [
        "acqui", "merger", "takeover", "buyout", "acquired",
        "definitive agreement to acquire",
    ],
}

# Cache: ticker → {catalyst, title, filed_at, confidence}
_catalyst_cache      = {}
_catalyst_cache_date = None
_catalyst_lock       = threading.Lock()

# SEC EDGAR 8-K RSS — updated every 10 minutes, completely free
_SEC_8K_RSS = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=100&search_text=&output=atom"
_SEC_HEADERS = {"User-Agent": "GapScanner/1.0 trading-research@example.com"}  # SEC requires User-Agent


def _classify_text(text):
    """Score text against catalyst keyword lists. Returns (catalyst_type, confidence 0-1)."""
    text_lower = text.lower()
    scores = {}
    for cat, keywords in _CATALYST_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text_lower)
        if hits:
            scores[cat] = hits / len(keywords)
    if not scores:
        return "news", 0.1
    best = max(scores, key=scores.get)
    return best, round(scores[best], 2)


def fetch_8k_catalysts(tickers):
    """
    Pull the SEC EDGAR 8-K RSS feed and match filings to our tickers.
    Looks back 24 hours so we catch pre-market filings from today.
    Returns dict: ticker → {catalyst, title, filed_at, confidence, url}
    """
    global _catalyst_cache, _catalyst_cache_date
    today = date.today()

    # Return cached result if already fetched today
    with _catalyst_lock:
        if _catalyst_cache_date == today and _catalyst_cache:
            print(f"  Catalysts: using cache ({len(_catalyst_cache)} entries)")
            return {t: _catalyst_cache.get(t) for t in tickers}

    print("  Catalysts: fetching SEC EDGAR 8-K RSS feed...")
    filings = []   # list of {tickers:[], title, filed_at, url, text}

    try:
        req  = Request(_SEC_8K_RSS, headers=_SEC_HEADERS)
        resp = urlopen(req, timeout=10)
        xml  = resp.read().decode("utf-8", errors="replace")

        # Parse Atom feed
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        for entry in root.findall("atom:entry", ns):
            try:
                title    = entry.findtext("atom:title",   default="", namespaces=ns)
                updated  = entry.findtext("atom:updated", default="", namespaces=ns)
                link_el  = entry.find("atom:link",        ns)
                url      = link_el.get("href", "") if link_el is not None else ""
                summary  = entry.findtext("atom:summary", default="", namespaces=ns)

                # Parse timestamp
                try:
                    filed_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except Exception:
                    continue
                if filed_dt < cutoff:
                    continue  # too old

                # Extract ticker from title — SEC format: "TICKER (0001234567) (8-K)"
                ticker_match = re.search(r"\b([A-Z]{1,5})\b", title)
                tickers_in   = re.findall(r"\b([A-Z]{2,5})\b", title)

                # Combined text for classification
                full_text = f"{title} {summary}"
                cat, conf = _classify_text(full_text)

                filings.append({
                    "tickers":   tickers_in,
                    "title":     title,
                    "filed_at":  updated,
                    "url":       url,
                    "catalyst":  cat,
                    "confidence":conf,
                })
            except Exception:
                continue

        print(f"  Catalysts: parsed {len(filings)} recent 8-K filings")

    except URLError as e:
        print(f"  Catalysts: SEC RSS fetch failed — {e}")
        return {t: None for t in tickers}
    except ET.ParseError as e:
        print(f"  Catalysts: XML parse error — {e}")
        return {t: None for t in tickers}
    except Exception as e:
        print(f"  Catalysts: unexpected error — {e}")
        return {t: None for t in tickers}

    # Match filings to our tickers
    results = {}
    for ticker in tickers:
        best = None
        for f in filings:
            if ticker in f["tickers"]:
                # Prefer higher-confidence / better catalyst types
                cat_rank = {"earnings": 0, "fda": 1, "acquisition": 2,
                            "contract": 3, "upgrade": 4, "news": 5}
                if best is None or cat_rank.get(f["catalyst"], 9) < cat_rank.get(best["catalyst"], 9):
                    best = f
        results[ticker] = best

    matched = sum(1 for v in results.values() if v)
    print(f"  Catalysts: matched {matched}/{len(tickers)} tickers to 8-K filings")

    # Update cache
    with _catalyst_lock:
        _catalyst_cache      = results
        _catalyst_cache_date = today

    return results


def determine_catalyst(ticker, yf_data, sec_filing, sector, gap_pct):
    """
    Final catalyst determination — priority order:
      1. SEC 8-K filing today (highest confidence — actual filing)
      2. yfinance earnings calendar (scheduled earnings)
      3. Sector + gap heuristic (biotech large gap = likely FDA)
      4. yfinance news headline
      5. Default to "news"
    Returns (catalyst_type, source, detail)
    """
    # 1. SEC 8-K
    if sec_filing:
        cat  = sec_filing["catalyst"]
        conf = sec_filing["confidence"]
        if conf >= 0.05:   # at least one keyword hit
            return cat, "sec_8k", sec_filing["title"][:80]

    # 2. yfinance earnings calendar
    if yf_data.get("has_earnings"):
        return "earnings", "yfinance_calendar", "Scheduled earnings today"

    # 3. Sector heuristic
    if sector in ("Healthcare", "Biotechnology") and gap_pct >= 8:
        return "fda", "heuristic", "Large gap on biotech — possible FDA/clinical"

    # 4. yfinance news headline
    headline = yf_data.get("yf_headline", "")
    if headline:
        return "news", "yfinance_news", headline

    # 5. Default
    return "news", "default", "No specific catalyst identified"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# yfinance helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fetch_one_yf(ticker):
    """Pull fundamentals for a single ticker. Returns (ticker, dict)."""
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        float_shares   = info.get("floatShares") or info.get("sharesOutstanding") or 0
        float_m        = round(float_shares / 1_000_000, 1) if float_shares else 150.0
        shares_out     = info.get("sharesOutstanding") or 0
        shares_out_m   = round(shares_out / 1_000_000, 1) if shares_out else None
        shares_short   = info.get("sharesShort") or 0
        shares_short_m = round(shares_short / 1_000_000, 2) if shares_short else None
        short_ratio    = round(info.get("shortRatio") or 0, 1) or None  # days to cover
        avg_vol        = info.get("averageVolume10days") or info.get("averageVolume") or 0
        sector         = info.get("sector") or "—"

        # Check if earnings fall today
        has_earnings = False
        try:
            cal = t.calendar
            if cal is not None and not (hasattr(cal, "empty") and cal.empty):
                # calendar can be a dict or DataFrame depending on yf version
                if hasattr(cal, "columns"):
                    earn_date = cal.columns[0]
                    has_earnings = (earn_date.date() == date.today())
                elif isinstance(cal, dict) and "Earnings Date" in cal:
                    earn_dates = cal["Earnings Date"]
                    if earn_dates:
                        has_earnings = (earn_dates[0].date() == date.today())
        except Exception:
            pass

        short_float = round((info.get("shortPercentOfFloat") or 0) * 100, 1)

        # Grab top 5 news headlines — yfinance structure: item["content"]["..."]
        yf_headline = ""
        yf_news     = []
        try:
            news = t.news or []
            now_ts = datetime.now(timezone.utc).timestamp()
            for item in news[:5]:
                content   = item.get("content") or {}
                title     = content.get("title") or ""
                if not title:
                    continue
                summary   = content.get("summary") or content.get("description") or ""
                publisher = (content.get("provider") or {}).get("displayName") or ""
                url       = (content.get("canonicalUrl") or {}).get("url") or \
                            (content.get("clickThroughUrl") or {}).get("url") or ""
                pub_date  = content.get("pubDate") or content.get("displayTime") or ""
                age_min   = None
                try:
                    if pub_date:
                        from datetime import datetime as dt2
                        pub_dt  = dt2.fromisoformat(pub_date.replace("Z", "+00:00"))
                        age_min = int((now_ts - pub_dt.timestamp()) / 60)
                except Exception:
                    pass
                yf_news.append({
                    "title":     title[:120],
                    "summary":   summary[:300],
                    "publisher": publisher[:40],
                    "url":       url,
                    "age_min":   age_min,
                })
            if yf_news:
                yf_headline = yf_news[0]["title"]
        except Exception as e:
            print(f"  yfinance news error for {ticker}: {e}")

        return ticker, {
            "float":          float_m,
            "shares_out_m":   shares_out_m,
            "shares_short_m": shares_short_m,
            "short_ratio":    short_ratio,
            "sector":         sector,
            "avg_vol":        avg_vol,
            "has_earnings":   has_earnings,
            "short_float":    short_float,
            "yf_headline":    yf_headline,
            "yf_news":        yf_news,
        }
    except Exception as e:
        return ticker, {"float": 150.0, "sector": "—", "avg_vol": 0,
                        "has_earnings": False, "short_float": 0.0, "yf_headline": "", "yf_news": [],
                        "shares_out_m": None, "shares_short_m": None, "short_ratio": None}


def enrich_with_yfinance(tickers):
    """
    Return yfinance fundamentals for a list of tickers.
    Results are cached per trading day; parallel fetch for speed.
    """
    global _yf_cache, _yf_cache_date
    today = date.today()

    with _yf_lock:
        if _yf_cache_date != today:
            print("  yfinance: new trading day — clearing cache")
            _yf_cache      = {}
            _yf_cache_date = today
        missing = [t for t in tickers if t not in _yf_cache]

    if missing:
        print(f"  yfinance: fetching {len(missing)} tickers in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for ticker, data in pool.map(_fetch_one_yf, missing):
                with _yf_lock:
                    _yf_cache[ticker] = data
        print(f"  yfinance: done ({len(_yf_cache)} total cached)")

    with _yf_lock:
        return {t: _yf_cache.get(t, {"float": 150.0, "sector": "—",
                                      "avg_vol": 0, "has_earnings": False})
                for t in tickers}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Scoring & trade sizing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def score_setup(gap_pct, rel_vol, float_m, catalyst):
    # Relative volume (35%) -- large caps rarely hit 2x pre-market, scaled down
    if rel_vol < 0.5:   vs = 0
    elif rel_vol < 1.0: vs = 25
    elif rel_vol < 1.5: vs = 50
    elif rel_vol < 2.5: vs = 75
    elif rel_vol < 4:   vs = 90
    else:               vs = 100

    # Catalyst quality (30%)
    cs = {"earnings": 100, "fda": 90, "contract": 70, "upgrade": 55, "news": 35}.get(catalyst, 35)

    # Float (20%)
    if float_m < 20:    fs = 100
    elif float_m < 30:  fs = 90
    elif float_m < 50:  fs = 75
    elif float_m < 80:  fs = 60
    elif float_m < 150: fs = 40
    elif float_m < 300: fs = 25
    else:               fs = 10

    # Gap sweet spot 4-30% (15%) -- removed hard penalty over 25%
    if gap_pct < 3:        gs = 20
    elif gap_pct < 4:      gs = 50
    elif gap_pct <= 20:    gs = min(100, gap_pct * 5)
    elif gap_pct <= 30:    gs = 80
    else:                  gs = 55

    total = round(vs * 0.35 + cs * 0.30 + fs * 0.20 + gs * 0.15)

    checks = {
        "relVol":      rel_vol >= 1.0,
        "catalyst":    catalyst in ("earnings", "fda", "contract", "upgrade", "news"),
        "float":       float_m < 100,   # info only — not used in passes
        "gap":         4 <= gap_pct <= 30,
        "gapExtended": gap_pct > 30,
    }
    passes = sum([checks["relVol"], checks["catalyst"], checks["gap"]])  # float removed

    if checks["gapExtended"]:  priority = "C"
    elif passes == 3:          priority = "A"
    elif passes == 2:          priority = "B"
    elif passes == 1:          priority = "C"
    else:                      priority = "SKIP"

    return {"total": total, "gap": round(gs), "vol": round(vs), "float": round(fs),
            "catalyst": round(cs), "priority": priority, "checks": checks,
            "relVol": round(rel_vol, 2)}


MAX_POSITION_PCT = 25.0  # Never more than 25% of account in one trade

def calc_trade(price, account_size, risk_pct):
    max_risk  = account_size * (risk_pct / 100)
    stop_dist = price * 0.0075
    shares    = int(max_risk / stop_dist) if stop_dist > 0 else 0

    # Hard cap: never exceed MAX_POSITION_PCT of account
    max_shares_by_exposure = int((account_size * MAX_POSITION_PCT / 100) / price) if price > 0 else 0
    shares    = min(shares, max_shares_by_exposure)

    pos_size  = round(shares * price, 2)
    pos_pct   = round((pos_size / account_size) * 100, 1) if account_size else 0
    capped    = shares == max_shares_by_exposure and max_shares_by_exposure < int(max_risk / stop_dist)
    return {
        "shares":  shares,
        "stop":    round(price - stop_dist, 2),
        "target1": round(price * 1.01, 2),
        "target2": round(price * 1.02, 2),
        "maxRisk": round(max_risk),
        "posSize": pos_size,
        "posPct":  pos_pct,
        "capped":  capped,   # True if position was reduced due to exposure cap
    }



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Technical indicators — pulled from yfinance daily bars
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1 - k)
    return round(val, 4)

def _sma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 4)

def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 1)

def fetch_technicals(ticker, current_price):
    try:
        t = yf.Ticker(ticker)
        print(f"  [tech] {ticker}: calling history()...")
        hist = t.history(period="1y", interval="1d", auto_adjust=True)
        print(f"  [tech] {ticker}: got {len(hist) if hist is not None else 'None'} rows, empty={hist.empty if hist is not None else 'N/A'}")
        if hist is None or hist.empty:
            hist = t.history(period="6mo", interval="1d", auto_adjust=True)
            print(f"  [tech] {ticker}: 6mo fallback: {len(hist) if hist is not None else 'None'} rows")
        if hist is None or hist.empty or len(hist) < 20:
            print(f"  [tech] {ticker}: FAILED — not enough data")
            return None
        closes = list(hist["Close"].astype(float))
        print(f"  [tech] {ticker}: ✅ {len(closes)} closes, last={closes[-1]:.2f}")
        ema8   = _ema(closes, 8)
        ema21  = _ema(closes, 21)
        sma50  = _sma(closes, 50)
        sma200 = _sma(closes, 200)
        rsi    = _rsi(closes)

        def vs_ma(ma):
            if ma is None: return None
            return round(((current_price - ma) / ma) * 100, 2)

        mas   = [ema8, ema21, sma50, sma200]
        above = sum(1 for m in mas if m and current_price > m)

        if rsi is None:   rsi_zone = "unknown"
        elif rsi >= 70:   rsi_zone = "overbought"
        elif rsi >= 60:   rsi_zone = "hot"
        elif rsi >= 40:   rsi_zone = "neutral"
        elif rsi >= 30:   rsi_zone = "cooling"
        else:             rsi_zone = "oversold"

        return {
            "ema8": ema8, "ema21": ema21, "sma50": sma50, "sma200": sma200,
            "rsi": rsi, "rsiZone": rsi_zone,
            "vsEma8": vs_ma(ema8), "vsEma21": vs_ma(ema21),
            "vsSma50": vs_ma(sma50), "vsSma200": vs_ma(sma200),
            "masAbove": above,
        }
    except Exception as e:
        print(f"  technicals error {ticker}: {e}")
        return None

def fetch_technicals_parallel(candidates):
    result = {}
    print(f"  Technicals: fetching {len(candidates)} tickers...")
    # Keep workers low (4) to avoid yfinance rate limiting
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_technicals, c["ticker"], c["price"]): c["ticker"]
                   for c in candidates}
        for f in concurrent.futures.as_completed(futures):
            ticker = futures[f]
            try:
                result[ticker] = f.result()
            except Exception as e:
                print(f"  technicals thread error {ticker}: {e}")
                result[ticker] = None
    ok = sum(1 for v in result.values() if v)
    print(f"  Technicals: {ok}/{len(candidates)} succeeded")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main scan function
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_scan(filters):
    min_gap   = float(filters.get("minGap",   3.0))
    min_vol   = int(filters.get("minVol",     100_000))
    min_price = float(filters.get("minPrice", 3.0))
    max_price = float(filters.get("maxPrice", 500.0))
    acct      = float(filters.get("accountSize", 92_000))
    risk_pct  = float(filters.get("riskPct",  0.5))

    sep = "─" * 52
    print(f"\n{sep}")
    print(f"  SCAN  {datetime.now().strftime('%H:%M:%S CT')}  filters: gap≥{min_gap}% vol≥{min_vol:,}")
    print(sep)

    # ── 1. Alpaca snapshots ────────────────────────────────────────────────────
    client   = StockHistoricalDataClient(API_KEY, API_SECRET)
    universe, universe_source = get_universe(force_refresh=True)
    print(f"  Universe: {len(universe)} tickers  [{universe_source}]")

    # Alpaca snapshot limit is 1000 — chunk if needed
    all_snapshots = {}
    chunk_size = 500
    for i in range(0, len(universe), chunk_size):
        chunk = universe[i:i+chunk_size]
        try:
            req   = StockSnapshotRequest(symbol_or_symbols=chunk)
            chunk_snaps = client.get_stock_snapshot(req)
            all_snapshots.update(chunk_snaps)
        except Exception as e:
            print(f"  ⚠  Snapshot chunk {i//chunk_size+1} error: {e}")
    snapshots = all_snapshots
    print(f"  Alpaca: {len(snapshots)} snapshots received")

    # ── 2. Filter for gappers ─────────────────────────────────────────────────
    skipped    = {}
    candidates = []

    for ticker, snap in snapshots.items():
        # Price
        price = 0.0
        if snap.latest_quote:
            ask = float(snap.latest_quote.ask_price or 0)
            bid = float(snap.latest_quote.bid_price or 0)
            if ask > 0 and bid > 0:
                price = round((ask + bid) / 2, 2)
        if price <= 0 and snap.latest_trade:
            price = float(snap.latest_trade.price or 0)
        if price <= 0:
            skipped["no_price"] = skipped.get("no_price", 0) + 1
            continue

        # Prev close (MUST be previous_daily_bar, not daily_bar)
        if not snap.previous_daily_bar:
            skipped["no_prev_bar"] = skipped.get("no_prev_bar", 0) + 1
            continue
        prev_close = float(snap.previous_daily_bar.close or 0)
        if prev_close <= 0:
            skipped["no_prev_close"] = skipped.get("no_prev_close", 0) + 1
            continue

        # Gap
        gap_pct = round(((price - prev_close) / prev_close) * 100, 2)
        if gap_pct < min_gap:
            skipped["gap_small"] = skipped.get("gap_small", 0) + 1
            continue

        # Price range
        if not (min_price <= price <= max_price):
            skipped["price_range"] = skipped.get("price_range", 0) + 1
            continue

        # Pre-market volume
        pm_vol = int(snap.daily_bar.volume or 0) if snap.daily_bar else 0
        if pm_vol <= 0 and snap.minute_bar:
            pm_vol = int(snap.minute_bar.volume or 0)
        if pm_vol < min_vol:
            skipped["vol_low"] = skipped.get("vol_low", 0) + 1
            continue

        prev_vol   = int(snap.previous_daily_bar.volume or 1)
        # Spread tightness — already have bid/ask, compute it free
        ask_p = float(snap.latest_quote.ask_price or 0) if snap.latest_quote else 0
        bid_p = float(snap.latest_quote.bid_price or 0) if snap.latest_quote else 0
        spread_pct = round(((ask_p - bid_p) / price) * 100, 3) if (ask_p and bid_p and price) else None

        # VWAP — from daily_bar (pre-market session VWAP), fallback to minute_bar
        vwap = None
        if snap.daily_bar and snap.daily_bar.vwap:
            vwap = round(float(snap.daily_bar.vwap), 2)
        elif snap.minute_bar and snap.minute_bar.vwap:
            vwap = round(float(snap.minute_bar.vwap), 2)

        # Pre-market high + day open — free from daily_bar
        pm_high  = round(float(snap.daily_bar.high  or 0), 2) if snap.daily_bar else None
        day_open = round(float(snap.daily_bar.open  or 0), 2) if snap.daily_bar else None
        if pm_high  == 0: pm_high  = None
        if day_open == 0: day_open = None

        candidates.append({
            "ticker": ticker, "price": price, "prevClose": prev_close,
            "pmVol": pm_vol, "prevVol": prev_vol, "gap": gap_pct,
            "spreadPct": spread_pct, "vwap": vwap,
            "pmHigh": pm_high, "dayOpen": day_open,
        })

    print(f"  Alpaca filter: {len(candidates)} candidates  |  skipped: {skipped}")

    if not candidates:
        return [], {"alpaca_count": len(snapshots), "yf_enriched": 0,
                    "earnings_today": [], "skipped": skipped}

    # ── 3. yfinance enrichment ────────────────────────────────────────────────
    cand_tickers = [c["ticker"] for c in candidates]
    yf_data = enrich_with_yfinance(cand_tickers) if YF_OK else \
              {t: {"float": 150.0, "sector": "—", "avg_vol": 0, "has_earnings": False}
               for t in cand_tickers}

    earnings_today = [t for t, d in yf_data.items() if d.get("has_earnings")]
    if earnings_today:
        print(f"  Earnings today: {', '.join(earnings_today)}")

    # ── 4. SEC EDGAR catalyst detection ──────────────────────────────────────────
    sec_catalysts = fetch_8k_catalysts(cand_tickers)

    # ── 5. Technical indicators (parallel yfinance history fetch) ─────────────
    tech_data = fetch_technicals_parallel(candidates) if YF_OK else {}

    # ── 6. Score and build results ────────────────────────────────────────────
    results = []
    for c in candidates:
        ticker   = c["ticker"]
        yfd      = yf_data.get(ticker, {})
        float_m  = yfd.get("float", 150.0)
        sector   = yfd.get("sector", "—")
        avg_vol  = yfd.get("avg_vol") or c["prevVol"]

        # Catalyst — from SEC 8-K or yfinance calendar
        sec_filing           = sec_catalysts.get(ticker)
        catalyst, cat_source, cat_detail = determine_catalyst(
            ticker, yfd, sec_filing, sector, c["gap"]
        )

        # Use yfinance avg vol for more accurate rel_vol
        rel_vol = round(c["pmVol"] / max(avg_vol * 0.15, 1), 2)
        rel_vol = min(rel_vol, 20.0)

        # Spread tightness (from Alpaca bid/ask — stored in candidate)
        spread_pct = c.get("spreadPct", None)

        # VWAP position
        vwap        = c.get("vwap")
        vwap_vs_pct = round(((c["price"] - vwap) / vwap) * 100, 2) if vwap else None
        above_vwap  = vwap_vs_pct > 0 if vwap_vs_pct is not None else None

        # PM high / day open — already extracted from daily_bar
        pm_high     = c.get("pmHigh")
        day_open    = c.get("dayOpen")
        short_float    = yfd.get("short_float", 0.0)
        shares_out_m   = yfd.get("shares_out_m")
        shares_short_m = yfd.get("shares_short_m")
        short_ratio    = yfd.get("short_ratio")

        # ── Trade verdict ──────────────────────────────────────────────────────
        # GO:   A/B rank + above VWAP + rel_vol ≥2x + tight spread
        # WATCH: B/C rank or one condition missing
        # SKIP:  C rank or rel_vol <1x or very wide spread
        s        = score_setup(c["gap"], rel_vol, float_m, catalyst)
        rank_val = s.get("priority", "C")
        go_conditions = [
            rank_val in ("A", "B"),
            above_vwap is True,
            rel_vol >= 1.0,                                     # was 2.0 — too strict for large caps
            spread_pct is not None and spread_pct < 0.5,
        ]
        go_count = sum(go_conditions)
        if rank_val == "A" and go_count >= 3:
            verdict = "GO"
        elif rank_val in ("A","B") and go_count >= 2:
            verdict = "WATCH"
        elif rank_val == "C" and go_count >= 3:                 # strong setup even with C rank
            verdict = "WATCH"
        else:
            verdict = "SKIP"

        results.append({
            "ticker":      ticker,
            "price":       c["price"],
            "prevClose":   c["prevClose"],
            "pmVol":       c["pmVol"],
            "avgVol":      avg_vol,
            "relVol":      rel_vol,
            "gap":         c["gap"],
            "float":       float_m,
            "sector":      sector,
            "vwap":        vwap,
            "vwapVsPct":   vwap_vs_pct,
            "aboveVwap":   above_vwap,
            "pmHigh":      pm_high,
            "dayOpen":     day_open,
            "shortFloat":    short_float,
            "sharesOutM":    shares_out_m,
            "sharesShortM":  shares_short_m,
            "shortRatio":    short_ratio,
            "verdict":     verdict,
            "catalyst":    catalyst,
            "catSource":   cat_source,
            "catDetail":   cat_detail,
            "secFiling":   {"title": sec_filing["title"][:80], "url": sec_filing["url"],
                            "filed_at": sec_filing["filed_at"]} if sec_filing else None,
            "yfNews":      yf_data.get("yf_news", []),
            "spreadPct":   spread_pct,
            "technicals":  tech_data.get(ticker),
            "score":       score_setup(c["gap"], rel_vol, float_m, catalyst),
            "trade":       calc_trade(c["price"], acct, risk_pct),
            "dataSource":  "alpaca+yfinance+sec",
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

    # Sort: A first, then score descending
    pord = {"A": 0, "B": 1, "C": 2, "SKIP": 3}
    results.sort(key=lambda x: (pord.get(x["score"]["priority"], 4), -x["score"]["total"]))

    meta = {"alpaca_count": len(snapshots), "yf_enriched": len(cand_tickers),
            "earnings_today": earnings_today, "skipped": skipped,
            "universe_size": len(universe), "universe_source": universe_source}
    print(f"  ✅ {len(results)} gappers  |  {len(earnings_today)} earnings today")
    print(f"{sep}\n")
    return results, meta


def run_scan_background(filters):
    with _cache_lock:
        _cache["status"] = "scanning"
        _cache["error"]  = None
    try:
        results, meta = run_scan(filters)
        with _cache_lock:
            _cache["results"]      = results
            _cache["last_updated"] = datetime.now(timezone.utc).isoformat()
            _cache["status"]       = "done"
            _cache["meta"]         = meta
    except Exception as e:
        with _cache_lock:
            _cache["status"] = "error"
            _cache["error"]  = str(e)
        print(f"❌ Scan error: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API routes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route("/api/scan", methods=["POST"])
def api_scan():
    filters = request.get_json(silent=True) or {}
    threading.Thread(target=run_scan_background, args=(filters,), daemon=True).start()
    return jsonify({"status": "scanning"})

@app.route("/api/results", methods=["GET"])
def api_results():
    with _cache_lock:
        return jsonify({
            "status":       _cache["status"],
            "last_updated": _cache["last_updated"],
            "count":        len(_cache["results"]),
            "results":      _cache["results"],
            "meta":         _cache["meta"],
            "error":        _cache["error"],
        })

@app.route("/api/status", methods=["GET"])
def api_status():
    with _cache_lock:
        status = _cache["status"]
    return jsonify({
        "status":          status,
        "alpaca_ready":    ALPACA_OK and bool(API_KEY),
        "trading_ready":   TRADING_OK and bool(API_KEY),
        "yfinance_ready":  YF_OK,
        "keys_configured": bool(API_KEY and API_SECRET),
        "paper":           IS_PAPER,
        "watchlist_size":  len(WATCHLIST),
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
    })

@app.route("/api/diagnostic", methods=["GET"])
def api_diagnostic():
    """http://localhost:5001/api/diagnostic — raw data for first 5 tickers"""
    if not ALPACA_OK or not API_KEY:
        return jsonify({"error": "Alpaca not configured"}), 400
    try:
        client    = StockHistoricalDataClient(API_KEY, API_SECRET)
        sample    = WATCHLIST[:5]
        snapshots = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=sample))
        out = {}
        for ticker, snap in snapshots.items():
            ask = float(snap.latest_quote.ask_price or 0) if snap.latest_quote else 0
            bid = float(snap.latest_quote.bid_price or 0) if snap.latest_quote else 0
            pc  = float(snap.previous_daily_bar.close or 0)   if snap.previous_daily_bar else 0
            pmv = int(snap.daily_bar.volume or 0)          if snap.daily_bar else 0
            out[ticker] = {
                "ask": ask, "bid": bid,
                "mid": round((ask + bid) / 2, 2) if ask and bid else 0,
                "prev_close": pc, "pm_vol": pmv,
                "gap_pct": round((((ask+bid)/2 - pc) / pc) * 100, 2) if pc and ask and bid else None,
            }
        if YF_OK:
            _, yfd = _fetch_one_yf(sample[0])
            out[sample[0]]["yfinance"] = yfd
        return jsonify({"data": out, "time_utc": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "scanner.html")

@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True})


@app.route("/api/test-catalysts", methods=["GET"])
def api_test_catalysts():
    """
    Debug endpoint — tests SEC EDGAR 8-K catalyst detection.
    Open: http://localhost:5001/api/test-catalysts
    Optional: ?tickers=AAPL,TSLA,NVDA
    """
    tickers = request.args.get("tickers", "AAPL,TSLA,NVDA,AXSM,HIMS").upper().split(",")
    try:
        results = fetch_8k_catalysts(tickers)
        return jsonify({
            "tickers_checked": tickers,
            "matches":  {t: v for t, v in results.items() if v},
            "no_match": [t for t, v in results.items() if not v],
            "note": "Checks last 24hr of SEC EDGAR 8-K filings",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/test-news", methods=["GET"])
def api_test_news():
    """
    Debug endpoint — shows raw yfinance news for a ticker.
    Open: http://localhost:5001/api/test-news?ticker=RIOT
    """
    ticker = request.args.get("ticker", "RIOT").upper()
    try:
        import yfinance as yf, json as _json
        t    = yf.Ticker(ticker)
        news = t.news or []
        return jsonify({
            "ticker":      ticker,
            "count":       len(news),
            "raw_first":   news[0] if news else None,
            "all_titles":  [n.get("title") or (n.get("content") or {}).get("title") for n in news[:5]],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



    """
    Debug endpoint — tests yfinance history fetch for a single ticker.
    Open: http://localhost:5001/api/test-technicals?ticker=AAPL
    Shows exactly what yfinance returns and any errors.
    """
    ticker = request.args.get("ticker", "AAPL").upper()
    result = {"ticker": ticker, "steps": []}

    if not YF_OK:
        return jsonify({"error": "yfinance not installed"}), 400

    try:
        import yfinance as _yf
        result["yf_version"] = _yf.__version__

        # Step 1: basic info
        try:
            t    = _yf.Ticker(ticker)
            info = t.info
            result["steps"].append({
                "step": "info",
                "ok": True,
                "sector": info.get("sector"),
                "float":  info.get("floatShares"),
            })
        except Exception as e:
            result["steps"].append({"step": "info", "ok": False, "error": str(e)})

        # Step 2: 1y history
        try:
            t    = _yf.Ticker(ticker)
            hist = t.history(period="1y", interval="1d", auto_adjust=True)
            rows = len(hist) if hist is not None else 0
            result["steps"].append({
                "step":       "history_1y",
                "ok":         rows >= 20,
                "rows":       rows,
                "is_empty":   hist.empty if hist is not None else True,
                "last_close": float(hist["Close"].iloc[-1]) if rows > 0 else None,
                "columns":    list(hist.columns) if hist is not None and not hist.empty else [],
            })
        except Exception as e:
            result["steps"].append({"step": "history_1y", "ok": False, "error": str(e), "type": type(e).__name__})

        # Step 3: full technicals
        try:
            tech = fetch_technicals(ticker, 50.0)  # dummy price
            result["steps"].append({
                "step": "fetch_technicals",
                "ok":   tech is not None,
                "data": tech,
            })
        except Exception as e:
            result["steps"].append({"step": "fetch_technicals", "ok": False, "error": str(e)})

        result["verdict"] = "✅ WORKING" if all(s["ok"] for s in result["steps"]) else "❌ FAILING — see steps above"

    except Exception as e:
        result["fatal"] = str(e)

    return jsonify(result)



@app.route("/api/quote/<ticker>", methods=["GET"])
def api_quote(ticker):
    """
    Full quote + technicals for any ticker.
    Returns price, bid, ask, volume, change, prev_close,
    VWAP, RSI, EMA8/21, SMA50/200, float, sector, short float.
    """
    ticker = ticker.upper().strip()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    try:
        result = {}

        # ── Alpaca snapshot ───────────────────────────────────────────────────
        if ALPACA_OK and API_KEY:
            try:
                client = StockHistoricalDataClient(API_KEY, API_SECRET)
                snaps  = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=[ticker]))
                snap   = snaps.get(ticker)
                if snap:
                    price = 0.0
                    if snap.latest_quote:
                        ask = float(snap.latest_quote.ask_price or 0)
                        bid = float(snap.latest_quote.bid_price or 0)
                        if ask > 0 and bid > 0:
                            price = round((ask + bid) / 2, 2)
                        result["bid"] = round(bid, 2)
                        result["ask"] = round(ask, 2)
                    if price <= 0 and snap.latest_trade:
                        price = float(snap.latest_trade.price or 0)
                    if snap.daily_bar:
                        db = snap.daily_bar
                        result["volume"]    = int(db.volume or 0)
                        result["day_open"]  = round(float(db.open), 2)
                        result["day_high"]  = round(float(db.high), 2)
                        result["day_low"]   = round(float(db.low), 2)
                        result["day_vwap"]  = round(float(db.vwap), 2) if db.vwap else None
                    if snap.previous_daily_bar:
                        pc = float(snap.previous_daily_bar.close)
                        result["prev_close"] = round(pc, 2)
                        if price and pc:
                            chg = round(price - pc, 2)
                            result["change"]     = chg
                            result["change_pct"] = round(chg / pc * 100, 2)
                    result["price"] = round(price, 2)
                    # VWAP position
                    vwap = result.get("day_vwap")
                    if vwap and price:
                        result["above_vwap"]   = price > vwap
                        result["vwap_vs_pct"]  = round((price - vwap) / vwap * 100, 2)
            except Exception as e:
                result["alpaca_err"] = str(e)

        # ── yfinance price fallback ───────────────────────────────────────────
        if YF_OK and result.get("price", 0) <= 0:
            try:
                fi = yf.Ticker(ticker).fast_info
                result["price"]  = round(float(fi.last_price or 0), 2)
                result["volume"] = int(fi.three_month_average_volume or 0)
            except Exception:
                pass

        if result.get("price", 0) <= 0:
            return jsonify({"error": f"No data found for {ticker}"}), 404

        price = result["price"]

        # ── yfinance fundamentals + info ──────────────────────────────────────
        if YF_OK:
            try:
                t    = yf.Ticker(ticker)
                info = t.info
                result["name"]        = info.get("shortName") or info.get("longName") or ticker
                result["sector"]      = info.get("sector", "")
                result["industry"]    = info.get("industry", "")
                result["market_cap"]  = info.get("marketCap")
                result["float_shares"]= info.get("floatShares")
                result["short_float"] = round(info.get("shortPercentOfFloat", 0) * 100, 1) if info.get("shortPercentOfFloat") else None
                result["avg_vol_30d"] = info.get("averageVolume", 0)
                result["pe"]          = info.get("trailingPE")
                result["52w_high"]    = info.get("fiftyTwoWeekHigh")
                result["52w_low"]     = info.get("fiftyTwoWeekLow")
                # Relative volume
                avg = result.get("avg_vol_30d", 0)
                vol = result.get("volume", 0)
                result["rel_vol"] = round(vol / avg, 2) if avg and vol else None
            except Exception:
                result["name"] = ticker

        # ── Technicals: EMA8/21, SMA50/200, RSI ──────────────────────────────
        if YF_OK:
            tech = fetch_technicals(ticker, price)
            result["technicals"] = tech

        result["ticker"] = ticker
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Trading API endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.route("/api/account", methods=["GET"])
def api_account():
    if not TRADING_OK or not API_KEY:
        return jsonify({"error": "Trading not configured"}), 400
    try:
        a = _tc().get_account()
        return jsonify({
            "equity":         float(a.equity),
            "cash":           float(a.cash),
            "buying_power":   float(a.buying_power),
            "daytrade_count": int(a.daytrade_count),
            "pdt":            bool(a.pattern_day_trader),
            "paper":          IS_PAPER,
            "status":         a.status.value if hasattr(a.status, "value") else str(a.status),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/positions", methods=["GET"])
def api_positions():
    if not TRADING_OK or not API_KEY:
        return jsonify({"positions": []}), 400
    try:
        positions = _tc().get_all_positions()
        out = []
        for p in positions:
            entry  = float(p.avg_entry_price or 0)
            cur    = float(p.current_price   or 0)
            qty    = float(p.qty             or 0)
            pl     = float(p.unrealized_pl   or 0)
            plpct  = float(p.unrealized_plpc or 0) * 100
            out.append({
                "ticker":      p.symbol,
                "qty":         qty,
                "side":        p.side.value if hasattr(p.side,"value") else str(p.side),
                "entry":       round(entry, 2),
                "price":       round(cur,   2),
                "value":       round(float(p.market_value or 0), 2),
                "pl":          round(pl,    2),
                "pl_pct":      round(plpct, 2),
                "asset_class": p.asset_class.value if hasattr(p.asset_class,"value") else "us_equity",
            })
        total_pl = sum(p["pl"] for p in out)
        return jsonify({"positions": out, "count": len(out), "total_pl": round(total_pl, 2)})
    except Exception as e:
        return jsonify({"positions": [], "error": str(e)}), 500


@app.route("/api/orders", methods=["GET"])
def api_orders():
    if not TRADING_OK or not API_KEY:
        return jsonify({"orders": []}), 400
    try:
        req    = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=20)
        orders = _tc().get_orders(filter=req)
        out = []
        for o in orders:
            out.append({
                "id":          str(o.id),
                "ticker":      o.symbol,
                "side":        o.side.value,
                "type":        o.order_type.value,
                "qty":         float(o.qty or 0),
                "filled_qty":  float(o.filled_qty or 0),
                "limit_price": float(o.limit_price) if o.limit_price else None,
                "stop_price":  float(o.stop_price)  if o.stop_price  else None,
                "status":      o.status.value,
                "created_at":  o.created_at.isoformat() if o.created_at else None,
                "legs":        len(o.legs) if o.legs else 0,
            })
        return jsonify({"orders": out, "count": len(out)})
    except Exception as e:
        return jsonify({"orders": [], "error": str(e)}), 500


@app.route("/api/order", methods=["POST"])
def api_order():
    """
    Place an order. Supports:
      BUY  — bracket order (entry + stop-loss + take-profit in one shot)
        body: { ticker, qty, side:"buy", stop_loss, take_profit, limit_price? }
      SELL — market sell to close (simple, immediate)
        body: { ticker, qty, side:"sell" }
    """
    if not TRADING_OK or not API_KEY:
        return jsonify({"ok": False, "error": "Trading not configured"}), 400

    body        = request.get_json(silent=True) or {}
    ticker      = body.get("ticker", "").upper()
    qty         = int(body.get("qty", 0))
    side        = body.get("side", "buy").lower()
    stop_loss   = float(body.get("stop_loss",   0) or 0)
    take_profit = float(body.get("take_profit", 0) or 0)
    limit_price = body.get("limit_price")

    if not ticker or qty <= 0:
        return jsonify({"ok": False, "error": "ticker and qty required"}), 400

    try:
        tc = _tc()

        if side == "sell":
            # ── Simple market sell ────────────────────────────────────────
            order_req = MarketOrderRequest(
                symbol        = ticker,
                qty           = qty,
                side          = OrderSide.SELL,
                time_in_force = TimeInForce.DAY,
            )

        elif limit_price:
            # ── Limit bracket buy ─────────────────────────────────────────
            if not stop_loss or not take_profit:
                return jsonify({"ok": False, "error": "stop_loss and take_profit required for buy"}), 400
            order_req = LimitOrderRequest(
                symbol        = ticker,
                qty           = qty,
                side          = OrderSide.BUY,
                type          = OrderType.LIMIT,
                time_in_force = TimeInForce.DAY,
                limit_price   = round(float(limit_price), 2),
                order_class   = OrderClass.BRACKET,
                stop_loss     = {"stop_price":  round(stop_loss, 2)},
                take_profit   = {"limit_price": round(take_profit, 2)},
            )

        else:
            # ── Market bracket buy ────────────────────────────────────────
            if not stop_loss or not take_profit:
                return jsonify({"ok": False, "error": "stop_loss and take_profit required for buy"}), 400
            order_req = MarketOrderRequest(
                symbol        = ticker,
                qty           = qty,
                side          = OrderSide.BUY,
                time_in_force = TimeInForce.DAY,
                order_class   = OrderClass.BRACKET,
                stop_loss     = {"stop_price":  round(stop_loss, 2)},
                take_profit   = {"limit_price": round(take_profit, 2)},
            )

        order = tc.submit_order(order_req)
        order_id_str = str(order.id)

        # ── Register manual BUY in order monitor so P&L is tracked ──────────
        if side == "buy" and stop_loss and take_profit:
            entry_price = float(body.get("entry_price", 0) or 0)
            if entry_price <= 0:
                if limit_price:
                    entry_price = float(limit_price)
                else:
                    try:
                        from alpaca.data.historical import StockHistoricalDataClient
                        from alpaca.data.requests import StockLatestTradeRequest
                        dc = StockHistoricalDataClient(API_KEY, API_SECRET)
                        snap = dc.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
                        entry_price = float(snap[ticker].price)
                    except Exception:
                        entry_price = 0.0
            with _auto_lock:
                _auto_state["open_orders"][order_id_str] = {
                    "ticker":      ticker,
                    "shares":      qty,
                    "entry_price": entry_price,
                    "stop":        stop_loss,
                    "target1":     take_profit,
                    "status":      "open",
                    "source":      "manual",
                }
            print(f"  📋 Manual order registered for P&L tracking: {ticker} {qty}sh @ ${entry_price:.2f}")

        return jsonify({
            "ok":       True,
            "order_id": order_id_str,
            "ticker":   order.symbol,
            "qty":      float(order.qty or 0),
            "side":     side,
            "status":   order.status.value  if hasattr(order.status,     "value") else str(order.status),
            "type":     order.order_type.value if hasattr(order.order_type, "value") else str(order.order_type),
            "paper":    IS_PAPER,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/order/<order_id>", methods=["DELETE"])
def api_cancel_order(order_id):
    """Cancel a specific open order."""
    if not TRADING_OK or not API_KEY:
        return jsonify({"error": "Trading not configured"}), 400
    try:
        _tc().cancel_order_by_id(order_id)
        return jsonify({"ok": True, "cancelled": order_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/position/<ticker>", methods=["DELETE"])
def api_close_position(ticker):
    """Flatten (market sell) an open position immediately."""
    if not TRADING_OK or not API_KEY:
        return jsonify({"error": "Trading not configured"}), 400
    try:
        resp = _tc().close_position(ticker.upper())
        return jsonify({
            "ok":     True,
            "ticker": ticker.upper(),
            "order_id": str(resp.id) if hasattr(resp, "id") else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/closeall", methods=["POST"])
def api_close_all():
    """Emergency: flatten ALL open positions."""
    if not TRADING_OK or not API_KEY:
        return jsonify({"error": "Trading not configured"}), 400
    try:
        _tc().close_all_positions(cancel_orders=True)
        return jsonify({"ok": True, "message": "All positions closed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Email Alerts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _send_email(subject, html_body, text_body=None):
    """Send alert email via Gmail SMTP. Returns (ok, error_msg)."""
    if not EMAIL_ENABLED:
        return False, "email disabled"
    if not all([EMAIL_FROM, EMAIL_APP_PASSWORD, EMAIL_TO]):
        return False, "email not configured in config.py"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Gap Scanner <{EMAIL_FROM}>"
        msg["To"]      = EMAIL_TO
        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        pw = EMAIL_APP_PASSWORD.replace(" ", "")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, pw)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"  📧 Email sent: {subject}")
        return True, None
    except Exception as e:
        print(f"  ❌ Email error: {e}")
        return False, str(e)


def _build_alert_email(results, new_tickers, scan_time_str):
    """Build HTML + plain-text email for alert."""
    go_list    = [r for r in results if r.get("verdict") == "GO"]
    watch_list = [r for r in results if r.get("verdict") == "WATCH"]
    new_list   = [r for r in results if r.get("ticker") in new_tickers]

    # Subject line
    counts = []
    if go_list:    counts.append(f"{len(go_list)} GO")
    if watch_list: counts.append(f"{len(watch_list)} WATCH")
    if new_list and ALERT_ON_NEW:
        new_not_alerted = [r for r in new_list if r.get("verdict") not in ("GO","WATCH")]
        if new_not_alerted: counts.append(f"{len(new_not_alerted)} new")
    subject = f"🚨 GAP SCANNER — {', '.join(counts) if counts else 'Scan complete'} [{scan_time_str} ET]"

    # Which tickers to include in email
    alert_set = set()
    if ALERT_ON_GO:    alert_set.update(r["ticker"] for r in go_list)
    if ALERT_ON_WATCH: alert_set.update(r["ticker"] for r in watch_list)
    if ALERT_ON_NEW:   alert_set.update(r["ticker"] for r in new_list)
    alert_results = [r for r in results if r["ticker"] in alert_set]

    if not alert_results:
        subject = f"📊 GAP SCANNER — No alerts [{scan_time_str} ET]"

    # ── HTML ──────────────────────────────────────────────────────────────────
    def verdict_badge(v):
        color = {"GO": "#00ff88", "WATCH": "#ffd700", "SKIP": "#555"}.get(v, "#555")
        bg    = {"GO": "rgba(0,255,136,0.15)", "WATCH": "rgba(255,215,0,0.12)", "SKIP": "transparent"}.get(v, "transparent")
        return f'<span style="padding:2px 8px;border:1px solid {color};color:{color};background:{bg};border-radius:3px;font-weight:700;font-size:13px">{v}</span>'

    def catalyst_badge(c):
        color = {"earnings":"#ff6b6b","fda":"#ff6b6b","contract":"#ffd700","upgrade":"#00bfff","news":"#00e5ff"}.get(c,"#888")
        return f'<span style="padding:1px 6px;border-radius:2px;background:{color}22;color:{color};font-size:11px;border:1px solid {color}44">{c.upper()}</span>'

    rows_html = ""
    for r in alert_results:
        t      = r.get("trade", {})
        is_new = r["ticker"] in new_tickers
        new_badge = ' <span style="font-size:10px;color:#00e5ff;padding:1px 4px;border:1px solid #00e5ff44;border-radius:2px">NEW</span>' if is_new else ""
        rows_html += f"""
        <tr style="border-bottom:1px solid #222">
          <td style="padding:12px 8px;white-space:nowrap">
            {verdict_badge(r.get("verdict","SKIP"))}&nbsp;&nbsp;
            <b style="font-size:16px;color:#e8eaf0">{r["ticker"]}</b>{new_badge}
          </td>
          <td style="padding:12px 8px;color:#00ff88;font-weight:700">+{r.get("gap",0):.1f}%</td>
          <td style="padding:12px 8px;color:#e8eaf0">${r.get("price",0):.2f}</td>
          <td style="padding:12px 8px">{catalyst_badge(r.get("catalyst","news"))}</td>
          <td style="padding:12px 8px;color:#aaa;font-size:12px">{r.get("float",0):.0f}M float</td>
          <td style="padding:12px 8px;color:#aaa;font-size:12px">{r.get("shortFloat",0):.1f}% short</td>
          <td style="padding:12px 8px">
            <span style="color:#aaa;font-size:11px">Stop </span><span style="color:#ff6b6b">${t.get("stop","—")}</span>
            &nbsp;
            <span style="color:#aaa;font-size:11px">T1 </span><span style="color:#00ff88">${t.get("target1","—")}</span>
          </td>
          <td style="padding:12px 8px;color:#aaa;font-size:12px">{r.get("score",{}).get("total",0)} score</td>
        </tr>"""

    if not rows_html:
        rows_html = '<tr><td colspan="8" style="padding:20px;color:#555;text-align:center">No GO/WATCH setups this scan</td></tr>'

    html = f"""<!DOCTYPE html>
<html><body style="background:#0d1117;color:#e8eaf0;font-family:'Courier New',monospace;margin:0;padding:20px">
<div style="max-width:780px;margin:0 auto">
  <div style="border-bottom:2px solid #00ff88;padding-bottom:12px;margin-bottom:20px">
    <span style="font-size:22px;font-weight:900;letter-spacing:3px;color:#00ff88">▸ GAP SCANNER</span>
    <span style="float:right;color:#555;font-size:12px;line-height:2.2">{scan_time_str} ET &nbsp;|&nbsp; {len(results)} gappers found</span>
  </div>
  <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse">
    <thead>
      <tr style="border-bottom:1px solid #333">
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">VERDICT / TICKER</th>
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">GAP</th>
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">PRICE</th>
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">CATALYST</th>
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">FLOAT</th>
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">SHORT</th>
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">LEVELS</th>
        <th style="padding:6px 8px;text-align:left;color:#555;font-size:11px;letter-spacing:1px">SCORE</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <div style="margin-top:24px;padding-top:12px;border-top:1px solid #222;color:#555;font-size:11px">
    <a href="http://localhost:5001" style="color:#00e5ff">Open Scanner ↗</a>
    &nbsp;&nbsp;|&nbsp;&nbsp; Auto-scan every {SCAN_INTERVAL_MIN}min &nbsp;|&nbsp; {SCAN_WINDOW_START}–{SCAN_WINDOW_END} ET
  </div>
</div>
</body></html>"""

    # ── Plain text fallback ───────────────────────────────────────────────────
    lines = [f"GAP SCANNER ALERT — {scan_time_str} ET", "="*50]
    for r in alert_results:
        t = r.get("trade", {})
        lines.append(f"\n[{r.get('verdict','SKIP')}] {r['ticker']}  +{r.get('gap',0):.1f}%  ${r.get('price',0):.2f}  {r.get('catalyst','').upper()}")
        lines.append(f"  Score:{r.get('score',{}).get('total',0)}  Float:{r.get('float',0):.0f}M  Short:{r.get('shortFloat',0):.1f}%")
        lines.append(f"  Stop:${t.get('stop','—')}  T1:${t.get('target1','—')}  T2:${t.get('target2','—')}")
    lines += ["", f"Open scanner: http://localhost:5001"]
    text = "\n".join(lines)

    return subject, html, text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Scheduler — auto-scan during market window
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_scheduler_state = {
    "last_scan_tickers": set(),   # tickers from previous scan cycle
    "last_alert_time":   None,    # avoid duplicate emails within same minute
    "scans_today":       0,
    "alerts_sent_today": 0,
}

def _in_scan_window():
    """True if current ET time is within configured scan window."""
    try:
        et = datetime.now(timezone(timedelta(hours=-5)))  # EST; fine for DST offset ±1h
        # Try pytz for proper EDT/EST handling
        try:
            import pytz
            et = datetime.now(pytz.timezone("America/New_York"))
        except ImportError:
            pass
        now_str  = et.strftime("%H:%M")
        is_wkday = et.weekday() < 5  # Mon–Fri only
        return is_wkday and SCAN_WINDOW_START <= now_str <= SCAN_WINDOW_END
    except Exception:
        return False


def _run_scheduled_scan():
    """Trigger a scan and send email alerts if warranted."""
    et = datetime.now(timezone(timedelta(hours=-5)))
    try:
        import pytz
        et = datetime.now(pytz.timezone("America/New_York"))
    except ImportError:
        pass
    scan_time_str = et.strftime("%I:%M %p").lstrip("0")
    print(f"\n⏰  Scheduled scan [{scan_time_str} ET] ...")

    # Run the scan (reuse existing scan logic)
    try:
        with app.test_request_context():
            resp = api_scan()
            data = resp.get_json() if hasattr(resp, "get_json") else {}
    except Exception as e:
        print(f"  Scheduled scan error: {e}")
        return

    results = data.get("results", [])
    _scheduler_state["scans_today"] += 1
    print(f"  → {len(results)} gappers found")

    # Determine which tickers are new vs last scan
    current_tickers = {r["ticker"] for r in results}
    prev_tickers    = _scheduler_state["last_scan_tickers"]
    new_tickers     = current_tickers - prev_tickers
    _scheduler_state["last_scan_tickers"] = current_tickers

    # Shadow log — queue GO/WATCH setups for 30-min outcome check
    _log_setups(results)

    # Auto-execution — place bracket orders for qualifying GO setups
    if AUTO_TRADE_ENABLED:
        _auto_execute(results)

    # Decide whether to send alert
    has_go    = ALERT_ON_GO    and any(r.get("verdict") == "GO"    for r in results)
    has_watch = ALERT_ON_WATCH and any(r.get("verdict") == "WATCH" for r in results)
    has_new   = ALERT_ON_NEW   and bool(new_tickers)

    if not (has_go or has_watch or has_new):
        print(f"  → No alert conditions met, skipping email")
        return

    # Deduplicate: don't send same-minute duplicate
    alert_key = et.strftime("%Y-%m-%d %H:%M")
    if _scheduler_state["last_alert_time"] == alert_key:
        print(f"  → Alert already sent this minute, skipping")
        return
    _scheduler_state["last_alert_time"] = alert_key

    subject, html, text = _build_alert_email(results, new_tickers, scan_time_str)
    ok, err = _send_email(subject, html, text)
    if ok:
        _scheduler_state["alerts_sent_today"] += 1


def _scheduler_loop():
    """Background thread — wakes every minute, scans if in window."""
    print(f"  🕐 Scheduler started — scanning {SCAN_WINDOW_START}–{SCAN_WINDOW_END} ET every {SCAN_INTERVAL_MIN}min")
    last_run = None
    while True:
        try:
            # Resolve any pending 30-min lookbacks
            _resolve_pending()

            if _in_scan_window():
                now_min = datetime.now().strftime("%Y-%m-%d %H:%M")
                # Only run once per SCAN_INTERVAL_MIN slot
                et = datetime.now(timezone(timedelta(hours=-5)))
                slot = et.minute // SCAN_INTERVAL_MIN
                slot_key = f"{et.strftime('%Y-%m-%d %H')}-{slot}"
                if slot_key != last_run:
                    last_run = slot_key
                    threading.Thread(target=_run_scheduled_scan, daemon=True).start()
        except Exception as e:
            print(f"  Scheduler loop error: {e}")
        time.sleep(30)  # check every 30s


def _start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()


# ── Manual email test endpoint ────────────────────────────────────────────────
@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    """Send a test email using current scan results."""
    with _cache_lock:
        results = _cache.get("results", [])
    if not results:
        return jsonify({"ok": False, "error": "No scan results — run a scan first"}), 400
    et = datetime.now(timezone(timedelta(hours=-5)))
    try:
        import pytz
        et = datetime.now(pytz.timezone("America/New_York"))
    except ImportError:
        pass
    scan_time_str = et.strftime("%I:%M %p").lstrip("0")
    subject, html, text = _build_alert_email(results, set(), scan_time_str + " [TEST]")
    ok, err = _send_email(subject, html, text)
    return jsonify({"ok": ok, "error": err, "to": EMAIL_TO if ok else None})



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Auto-Execution Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tracks auto-trade state across the session
_auto_state = {
    "trades_today":    0,
    "last_reset_day":  None,          # date string YYYY-MM-DD
    "open_orders":     {},            # order_id → trade_info dict
    "executed":        set(),         # tickers already traded today
    "daily_pnl":       0.0,          # realized P&L for today ($)
    "daily_trades":    [],            # list of {ticker, pnl, reason, time}
    "kill_switch":     False,         # True = auto-trade paused by loss limit
    "kill_reason":     "",            # why kill switch fired
}
_auto_lock = threading.Lock()


def _auto_reset_daily():
    """Reset daily counters if it's a new trading day."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _auto_lock:
        if _auto_state["last_reset_day"] != today:
            _auto_state["trades_today"]   = 0
            _auto_state["last_reset_day"] = today
            _auto_state["executed"]       = set()
            _auto_state["daily_pnl"]      = 0.0
            _auto_state["daily_trades"]   = []
            _auto_state["kill_switch"]    = False
            _auto_state["kill_reason"]    = ""
            print(f"  🔄 Auto-trade counters reset for {today}")


def _send_buy_email(ticker, shares, entry_price, stop, target1, gap_pct, catalyst, score, verdict):
    """Email notification when a bracket buy order is placed."""
    et = datetime.now(timezone(timedelta(hours=-5)))
    try:
        import pytz
        et = datetime.now(pytz.timezone("America/New_York"))
    except ImportError:
        pass
    time_str  = et.strftime("%I:%M %p").lstrip("0")
    pos_size  = round(shares * entry_price, 2)
    risk_amt  = round(shares * (entry_price - stop), 2)
    reward    = round(shares * (target1 - entry_price), 2)

    subject = f"🟢 AUTO-BUY {ticker}  {shares}sh @ ${entry_price:.2f}  [{time_str} ET]"
    html = f"""
    <div style="background:#0d1117;color:#e6edf3;font-family:monospace;padding:24px;border-radius:8px;max-width:520px">
      <div style="font-size:22px;font-weight:bold;letter-spacing:3px;color:#3fb950;margin-bottom:4px">
        🟢 AUTO-BUY EXECUTED</div>
      <div style="font-size:13px;color:#8b949e;margin-bottom:20px">{time_str} ET — Paper Account</div>

      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr><td style="padding:6px 0;color:#8b949e">Ticker</td>
            <td style="padding:6px 0;color:#58a6ff;font-size:20px;font-weight:bold">{ticker}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Shares</td>
            <td style="padding:6px 0">{shares} shares</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Entry Price</td>
            <td style="padding:6px 0;font-weight:bold">${entry_price:.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Position Size</td>
            <td style="padding:6px 0">${pos_size:,.2f}</td></tr>
        <tr style="border-top:1px solid #21262d">
          <td style="padding:8px 0;color:#8b949e">Stop Loss</td>
          <td style="padding:8px 0;color:#f85149;font-weight:bold">${stop:.2f}
            <span style="color:#8b949e;font-size:12px"> (risk ${risk_amt:.2f})</span></td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Target</td>
            <td style="padding:6px 0;color:#3fb950;font-weight:bold">${target1:.2f}
              <span style="color:#8b949e;font-size:12px"> (reward ${reward:.2f})</span></td></tr>
        <tr style="border-top:1px solid #21262d">
          <td style="padding:8px 0;color:#8b949e">Gap</td>
          <td style="padding:8px 0">{gap_pct:.1f}%</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Catalyst</td>
            <td style="padding:6px 0;color:#d29922">{catalyst or '—'}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Score / Verdict</td>
            <td style="padding:6px 0">{score} / <b style="color:#3fb950">{verdict}</b></td></tr>
      </table>
      <div style="margin-top:16px;font-size:11px;color:#484f58;border-top:1px solid #21262d;padding-top:12px">
        Bracket order placed — stop and target will execute automatically.</div>
    </div>"""
    text = f"AUTO-BUY {ticker}: {shares}sh @ ${entry_price:.2f}  stop=${stop:.2f}  target=${target1:.2f}  gap={gap_pct:.1f}%"
    _send_email(subject, html, text)


def _send_fill_email(ticker, side, shares, fill_price, entry_price, stop, target1,
                     order_id, fill_reason, trade_pnl=None, daily_pnl=None,
                     kill_fired=False, kill_msg="", source="auto"):
    """Email when a position is closed — stop hit or target hit."""
    et = datetime.now(timezone(timedelta(hours=-5)))
    try:
        import pytz
        et = datetime.now(pytz.timezone("America/New_York"))
    except ImportError:
        pass
    time_str = et.strftime("%I:%M %p").lstrip("0")

    pnl      = trade_pnl if trade_pnl is not None else round((fill_price - entry_price) * shares, 2)
    pnl_pct  = round((fill_price - entry_price) / entry_price * 100, 2) if entry_price else 0
    won      = pnl >= 0
    color    = "#3fb950" if won else "#f85149"
    icon     = "✅" if fill_reason == "target" else ("🛑" if fill_reason == "stop" else "📤")
    label    = "TARGET HIT" if fill_reason == "target" else ("STOPPED OUT" if fill_reason == "stop" else "POSITION CLOSED")
    src_tag  = "MANUAL TRADE" if source == "manual" else "AUTO TRADE"
    src_color= "#58a6ff" if source == "manual" else "#8b949e"

    kill_banner = ""
    if kill_fired:
        kill_color  = "#f85149" if "loss" in kill_msg.lower() else "#3fb950"
        kill_icon   = "🛑" if "loss" in kill_msg.lower() else "🏆"
        kill_banner = f"""
      <div style="background:{kill_color}22;border:1px solid {kill_color};border-radius:6px;
                  padding:12px;margin:16px 0;font-size:13px;color:{kill_color};font-weight:bold">
        {kill_icon} AUTO-TRADE PAUSED — {kill_msg}
      </div>"""

    daily_row = ""
    if daily_pnl is not None:
        daily_color = "#3fb950" if daily_pnl >= 0 else "#f85149"
        daily_row = f"""
        <tr style="border-top:2px solid #21262d">
          <td style="padding:8px 0;color:#8b949e">Day P&amp;L</td>
          <td style="padding:8px 0;font-size:18px;font-weight:bold;color:{daily_color}">
            {'+'if daily_pnl>=0 else ''}{daily_pnl:.2f}</td></tr>"""

    subject = f"{icon} {label} {ticker}  P&L: {'+'if won else ''}{pnl:.2f}  [{time_str} ET]"
    if kill_fired:
        subject = f"🛑 {subject} — AUTO-TRADE PAUSED"

    html = f"""
    <div style="background:#0d1117;color:#e6edf3;font-family:monospace;padding:24px;border-radius:8px;max-width:520px">
      <div style="font-size:22px;font-weight:bold;letter-spacing:3px;color:{color};margin-bottom:4px">
        {icon} {label}</div>
      <div style="font-size:13px;color:#8b949e;margin-bottom:4px">{time_str} ET — Paper Account</div>
      <div style="display:inline-block;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;
                  background:{src_color}22;border:1px solid {src_color};color:{src_color};margin-bottom:16px">
        {src_tag}</div>
      {kill_banner}
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr><td style="padding:6px 0;color:#8b949e">Ticker</td>
            <td style="padding:6px 0;color:#58a6ff;font-size:20px;font-weight:bold">{ticker}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Shares</td>
            <td style="padding:6px 0">{shares}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Entry Price</td>
            <td style="padding:6px 0">${entry_price:.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Exit Price</td>
            <td style="padding:6px 0;font-weight:bold">${fill_price:.2f}</td></tr>
        <tr style="border-top:1px solid #21262d">
          <td style="padding:8px 0;color:#8b949e">Trade P&amp;L</td>
          <td style="padding:8px 0;font-size:22px;font-weight:bold;color:{color}">
            {'+'if won else ''}{pnl:.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">P&amp;L %</td>
            <td style="padding:6px 0;color:{color}">{'+'if won else ''}{pnl_pct:.2f}%</td></tr>
        {daily_row}
        <tr style="border-top:1px solid #21262d">
          <td style="padding:8px 0;color:#8b949e">Stop was</td>
          <td style="padding:8px 0;color:#f85149">${stop:.2f}</td></tr>
        <tr><td style="padding:6px 0;color:#8b949e">Target was</td>
            <td style="padding:6px 0;color:#3fb950">${target1:.2f}</td></tr>
      </table>
      <div style="margin-top:16px;font-size:11px;color:#484f58;border-top:1px solid #21262d;padding-top:12px">
        Order ID: {order_id}</div>
    </div>"""
    text = f"{label} {ticker}: exit ${fill_price:.2f}  P&L {'+'if won else ''}{pnl:.2f} ({'+'if won else ''}{pnl_pct:.2f}%)"
    if daily_pnl is not None:
        text += f"  |  Day P&L: {'+'if daily_pnl>=0 else ''}{daily_pnl:.2f}"
    _send_email(subject, html, text)


def _auto_execute(results):
    """
    Called after each scheduled scan.
    Places bracket buy orders for qualifying GO setups above VWAP.
    Returns list of execution dicts for logging.
    """
    if not AUTO_TRADE_ENABLED:
        return []
    if not TRADING_OK:
        print("  ⚠  Auto-trade: trading not configured")
        return []

    _auto_reset_daily()

    with _auto_lock:
        trades_today = _auto_state["trades_today"]
        executed     = _auto_state["executed"]
        kill_switch  = _auto_state["kill_switch"]
        kill_reason  = _auto_state["kill_reason"]

    if kill_switch:
        print(f"  🛑 Auto-trade KILL SWITCH active: {kill_reason}")
        return []

    if trades_today >= AUTO_MAX_TRADES_PER_DAY:
        print(f"  🚫 Auto-trade: daily limit reached ({AUTO_MAX_TRADES_PER_DAY})")
        return []

    # Get open positions to avoid doubling up
    try:
        tc = _tc()
        open_positions = {p.symbol for p in tc.get_all_positions()}
    except Exception as e:
        print(f"  ⚠  Auto-trade: could not fetch positions: {e}")
        open_positions = set()

    placed = []
    for r in results:
        with _auto_lock:
            if _auto_state["trades_today"] >= AUTO_MAX_TRADES_PER_DAY:
                break

        ticker  = r.get("ticker", "")
        verdict = r.get("verdict", "")
        score   = r.get("score", {})

        # ── Filters ───────────────────────────────────────────────────────────
        if verdict not in AUTO_VERDICTS:
            continue
        if score.get("total", 0) < AUTO_MIN_SCORE:
            print(f"  ⏭  {ticker}: score {score.get('total',0)} < {AUTO_MIN_SCORE}, skip")
            continue
        if AUTO_REQUIRE_ABOVE_VWAP and not r.get("aboveVwap"):
            print(f"  ⏭  {ticker}: below VWAP, skip")
            continue
        if ticker in open_positions:
            print(f"  ⏭  {ticker}: already have position, skip")
            continue
        if ticker in executed:
            print(f"  ⏭  {ticker}: already traded today, skip")
            continue

        # ── Trade parameters ──────────────────────────────────────────────────
        trade       = r.get("trade", {})
        shares      = trade.get("shares", 0)
        stop        = trade.get("stop")
        target1     = trade.get("target1")
        entry_price = r.get("price", 0)
        gap_pct     = r.get("gap", 0)
        catalyst    = r.get("catalyst", "")

        if not shares or shares < 1:
            print(f"  ⏭  {ticker}: 0 shares calculated, skip")
            continue
        if not stop or not target1:
            print(f"  ⏭  {ticker}: missing stop/target, skip")
            continue

        # ── Place bracket market order ────────────────────────────────────────
        try:
            order_req = MarketOrderRequest(
                symbol        = ticker,
                qty           = shares,
                side          = OrderSide.BUY,
                time_in_force = TimeInForce.DAY,
                order_class   = OrderClass.BRACKET,
                stop_loss     = {"stop_price":  round(float(stop),    2)},
                take_profit   = {"limit_price": round(float(target1), 2)},
            )
            order = _tc().submit_order(order_req)
            order_id = str(order.id)

            print(f"  ✅ AUTO-BUY {ticker}: {shares}sh @ ~${entry_price:.2f}  stop=${stop:.2f}  target=${target1:.2f}  [{order_id[:8]}]")

            trade_info = {
                "ticker":      ticker,
                "order_id":    order_id,
                "shares":      shares,
                "entry_price": entry_price,
                "stop":        float(stop),
                "target1":     float(target1),
                "gap_pct":     gap_pct,
                "catalyst":    catalyst,
                "score":       score.get("total", 0),
                "verdict":     verdict,
                "placed_at":   datetime.now(timezone.utc).isoformat(),
                "status":      "open",
            }

            # Track state
            with _auto_lock:
                _auto_state["trades_today"] += 1
                _auto_state["executed"].add(ticker)
                _auto_state["open_orders"][order_id] = trade_info

            placed.append(trade_info)

            # Send buy email
            threading.Thread(
                target=_send_buy_email,
                args=(ticker, shares, entry_price, float(stop), float(target1),
                      gap_pct, catalyst, score.get("total", 0), verdict),
                daemon=True
            ).start()

        except Exception as e:
            print(f"  ❌ AUTO-BUY {ticker} failed: {e}")

    if placed:
        print(f"  📊 Auto-trade: {len(placed)} order(s) placed today ({_auto_state['trades_today']}/{AUTO_MAX_TRADES_PER_DAY})")

    return placed


def _monitor_orders():
    """
    Background thread — polls open auto-orders every AUTO_MONITOR_INTERVAL seconds.
    Detects fills/stops/cancels and sends email notifications.
    """
    print(f"  👁  Order monitor started (checking every {AUTO_MONITOR_INTERVAL}s)")
    while True:
        try:
            with _auto_lock:
                watching = dict(_auto_state["open_orders"])  # copy

            if watching and TRADING_OK:
                tc = _tc()
                for order_id, info in watching.items():
                    try:
                        order = tc.get_order_by_id(order_id)
                        status = order.status.value if hasattr(order.status, "value") else str(order.status)

                        if status in ("filled",):
                            # Main buy order filled — now watching legs
                            with _auto_lock:
                                if _auto_state["open_orders"].get(order_id, {}).get("status") == "open":
                                    _auto_state["open_orders"][order_id]["status"] = "filled"
                                    print(f"  ✅ Order filled: {info['ticker']} [{order_id[:8]}]")

                        elif status in ("replaced",):
                            # Bracket orders get replaced by child legs — mark filled, keep watching
                            with _auto_lock:
                                if _auto_state["open_orders"].get(order_id, {}).get("status") == "open":
                                    _auto_state["open_orders"][order_id]["status"] = "filled"
                                    print(f"  ✅ Bracket replaced→filled: {info['ticker']} [{order_id[:8]}]")

                        elif status in ("canceled", "expired"):
                            with _auto_lock:
                                _auto_state["open_orders"].pop(order_id, None)
                            print(f"  ↩  Order {status}: {info['ticker']} [{order_id[:8]}]")

                    except Exception as e:
                        # Order may have been replaced by bracket legs — check positions
                        try:
                            positions = {p.symbol: p for p in tc.get_all_positions()}
                            ticker = info["ticker"]
                            if ticker not in positions:
                                # Position is closed — fetch recent orders to determine exit price
                                closed = tc.get_orders(filter=GetOrdersRequest(
                                    symbols=[ticker],
                                    status=QueryOrderStatus.CLOSED,
                                    limit=5,
                                ))
                                exit_order = None
                                for o in closed:
                                    ostat = o.status.value if hasattr(o.status, "value") else str(o.status)
                                    oside = o.side.value  if hasattr(o.side,   "value") else str(o.side)
                                    if ostat == "filled" and oside == "sell":
                                        exit_order = o
                                        break

                                if exit_order:
                                    fill_price = float(exit_order.filled_avg_price or info["entry_price"])
                                    entry_p    = info["entry_price"]
                                    stop_p     = info["stop"]
                                    target_p   = info["target1"]
                                    shares_n   = info["shares"]

                                    # Determine reason
                                    if fill_price <= stop_p * 1.002:
                                        reason = "stop"
                                    elif fill_price >= target_p * 0.998:
                                        reason = "target"
                                    else:
                                        reason = "manual"

                                    trade_pnl = round((fill_price - entry_p) * shares_n, 2)
                                    print(f"  📤 Position closed: {ticker} @ ${fill_price:.2f} [{reason}]  P&L: ${trade_pnl:+.2f}")

                                    # Update daily P&L state
                                    with _auto_lock:
                                        _auto_state["daily_pnl"] = round(_auto_state["daily_pnl"] + trade_pnl, 2)
                                        _auto_state["daily_trades"].append({
                                            "ticker": ticker,
                                            "pnl":    trade_pnl,
                                            "reason": reason,
                                            "time":   datetime.now().strftime("%H:%M"),
                                            "entry":  entry_p,
                                            "exit":   fill_price,
                                            "shares": shares_n,
                                        })
                                        new_daily_pnl = _auto_state["daily_pnl"]

                                    # ── Kill switch check ──────────────────────
                                    kill_fired = False
                                    kill_msg   = ""
                                    if AUTO_DAILY_LOSS_LIMIT != 0 and new_daily_pnl <= AUTO_DAILY_LOSS_LIMIT:
                                        with _auto_lock:
                                            _auto_state["kill_switch"] = True
                                            _auto_state["kill_reason"] = f"Daily loss limit hit: ${new_daily_pnl:.2f} ≤ ${AUTO_DAILY_LOSS_LIMIT}"
                                        kill_fired = True
                                        kill_msg   = f"Daily loss limit hit: ${new_daily_pnl:.2f}"
                                        print(f"  🛑🛑 KILL SWITCH FIRED — {kill_msg}")

                                    if AUTO_DAILY_PROFIT_TARGET > 0 and new_daily_pnl >= AUTO_DAILY_PROFIT_TARGET:
                                        with _auto_lock:
                                            _auto_state["kill_switch"] = True
                                            _auto_state["kill_reason"] = f"Daily profit target hit: ${new_daily_pnl:.2f} ≥ ${AUTO_DAILY_PROFIT_TARGET}"
                                        kill_fired = True
                                        kill_msg   = f"Profit target hit: ${new_daily_pnl:.2f}"
                                        print(f"  🏆 PROFIT TARGET HIT — auto-trade paused")

                                    # Remove from tracking
                                    with _auto_lock:
                                        _auto_state["open_orders"].pop(order_id, None)

                                    # Send fill email (include kill switch alert if fired)
                                    threading.Thread(
                                        target=_send_fill_email,
                                        args=(ticker, "sell", shares_n, fill_price,
                                              entry_p, stop_p, target_p, order_id, reason,
                                              trade_pnl, new_daily_pnl, kill_fired, kill_msg,
                                              info.get("source", "auto")),
                                        daemon=True
                                    ).start()

                        except Exception as inner_e:
                            print(f"  Monitor check error {info['ticker']}: {inner_e}")

            # ── Sweep for any closed positions not in open_orders ─────────────
            # Catches manual trades placed before server restart or outside the scanner
            try:
                if TRADING_OK and EMAIL_OK:
                    with _auto_lock:
                        already_tracked = {info["ticker"] for info in _auto_state["open_orders"].values()}
                        already_logged  = {t["ticker"] for t in _auto_state["daily_trades"]}

                    today_str = datetime.now().strftime("%Y-%m-%d")
                    closed = _tc().get_orders(filter=GetOrdersRequest(
                        status=QueryOrderStatus.CLOSED,
                        limit=20,
                        after=datetime.strptime(today_str, "%Y-%m-%d").replace(
                            tzinfo=timezone(timedelta(hours=-5))),
                    ))
                    for o in closed:
                        ostat = o.status.value if hasattr(o.status, "value") else str(o.status)
                        oside = o.side.value   if hasattr(o.side,   "value") else str(o.side)
                        sym   = o.symbol
                        if ostat != "filled" or oside != "sell":
                            continue
                        if sym in already_tracked or sym in already_logged:
                            continue
                        # Found an untracked closed sell — compute P&L from avg prices
                        fill_price = float(o.filled_avg_price or 0)
                        qty        = float(o.filled_qty or o.qty or 0)
                        if not fill_price or not qty:
                            continue
                        # Try to find matching buy order for entry price
                        entry_price = 0.0
                        try:
                            buys = _tc().get_orders(filter=GetOrdersRequest(
                                symbols=[sym], status=QueryOrderStatus.CLOSED, limit=10,
                                after=datetime.strptime(today_str, "%Y-%m-%d").replace(
                                    tzinfo=timezone(timedelta(hours=-5))),
                            ))
                            for b in buys:
                                bstat = b.status.value if hasattr(b.status, "value") else str(b.status)
                                bside = b.side.value   if hasattr(b.side,   "value") else str(b.side)
                                if bstat == "filled" and bside == "buy":
                                    entry_price = float(b.filled_avg_price or 0)
                                    break
                        except Exception:
                            pass
                        if not entry_price:
                            continue

                        trade_pnl = round((fill_price - entry_price) * qty, 2)
                        reason    = "manual"
                        time_str  = datetime.now().strftime("%H:%M")
                        print(f"  📧 Sweep found untracked trade: {sym} P&L ${trade_pnl:+.2f} — sending email")

                        with _auto_lock:
                            _auto_state["daily_pnl"] = round(_auto_state["daily_pnl"] + trade_pnl, 2)
                            _auto_state["daily_trades"].append({
                                "ticker": sym, "pnl": trade_pnl, "reason": reason,
                                "time": time_str, "entry": entry_price,
                                "exit": fill_price, "shares": int(qty),
                            })
                            new_daily_pnl = _auto_state["daily_pnl"]

                        threading.Thread(
                            target=_send_fill_email,
                            args=(sym, "sell", int(qty), fill_price, entry_price,
                                  0, 0, str(o.id), reason,
                                  trade_pnl, new_daily_pnl, False, "", "manual"),
                            daemon=True
                        ).start()
            except Exception as sweep_e:
                pass  # sweep is best-effort

        except Exception as e:
            print(f"  Monitor loop error: {e}")

        time.sleep(AUTO_MONITOR_INTERVAL)


def _start_order_monitor():
    """Launch the order monitor as a daemon thread."""
    t = threading.Thread(target=_monitor_orders, daemon=True)
    t.start()


# ── Auto-trade status endpoint ────────────────────────────────────────────────
@app.route("/api/auto-trade/status")
def api_auto_trade_status():
    with _auto_lock:
        return jsonify({
            "enabled":        AUTO_TRADE_ENABLED,
            "trades_today":   _auto_state["trades_today"],
            "max_per_day":    AUTO_MAX_TRADES_PER_DAY,
            "executed":       list(_auto_state["executed"]),
            "open_orders":    len(_auto_state["open_orders"]),
            "require_vwap":   AUTO_REQUIRE_ABOVE_VWAP,
            "min_score":      AUTO_MIN_SCORE,
            "verdicts":       AUTO_VERDICTS,
            "paper":          IS_PAPER,
            "daily_pnl":      _auto_state["daily_pnl"],
            "daily_trades":   _auto_state["daily_trades"],
            "kill_switch":    _auto_state["kill_switch"],
            "kill_reason":    _auto_state["kill_reason"],
            "loss_limit":     AUTO_DAILY_LOSS_LIMIT,
            "profit_target":  AUTO_DAILY_PROFIT_TARGET,
        })


@app.route("/api/auto-trade/reset-kill", methods=["POST"])
def api_reset_kill_switch():
    """Manually re-enable auto-trading after kill switch fired."""
    with _auto_lock:
        _auto_state["kill_switch"] = False
        _auto_state["kill_reason"] = ""
    print("  ✅ Kill switch reset manually via API")
    return jsonify({"ok": True, "message": "Kill switch reset — auto-trading re-enabled"})


@app.route("/api/pnl")
def api_pnl():
    """
    Daily + weekly + monthly P&L from Alpaca portfolio history.
    Also returns today's auto-trade log.
    """
    if not TRADING_OK:
        return jsonify({"error": "trading not configured"}), 400
    try:
        tc = _tc()
        account = tc.get_account()
        equity     = float(account.equity or 0)
        last_eq    = float(account.last_equity or equity)
        day_pnl    = round(equity - last_eq, 2)
        day_pnl_pct= round((day_pnl / last_eq * 100) if last_eq else 0, 2)

        # Get portfolio history — 1 month daily bars
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        hist_req = GetPortfolioHistoryRequest(period="1M", timeframe="1D", extended_hours=False)
        hist = tc.get_portfolio_history(hist_req)

        history_points = []
        if hist and hist.timestamp:
            for i, ts in enumerate(hist.timestamp):
                pl = hist.profit_loss[i] if hist.profit_loss else 0
                eq = hist.equity[i]      if hist.equity      else 0
                history_points.append({
                    "date":   datetime.fromtimestamp(ts).strftime("%m/%d"),
                    "pnl":    round(float(pl or 0), 2),
                    "equity": round(float(eq or 0), 2),
                })

        with _auto_lock:
            today_trades = list(_auto_state["daily_trades"])
            today_pnl    = _auto_state["daily_pnl"]
            kill_switch  = _auto_state["kill_switch"]

        # Week/month rollup from history
        week_pnl  = sum(p["pnl"] for p in history_points[-5:])  if history_points else 0
        month_pnl = sum(p["pnl"] for p in history_points)        if history_points else 0

        return jsonify({
            "equity":        round(equity, 2),
            "day_pnl":       day_pnl,
            "day_pnl_pct":   day_pnl_pct,
            "week_pnl":      round(week_pnl, 2),
            "month_pnl":     round(month_pnl, 2),
            "today_trades":  today_trades,
            "today_auto_pnl":today_pnl,
            "kill_switch":   kill_switch,
            "loss_limit":    AUTO_DAILY_LOSS_LIMIT,
            "profit_target": AUTO_DAILY_PROFIT_TARGET,
            "history":       history_points[-30:],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/universe")
def api_universe():
    """Show current dynamic universe — useful for debugging."""
    with _universe_lock:
        tickers = list(_universe_cache["tickers"])
        source  = _universe_cache["source"]
        fetched = _universe_cache["fetched_at"].isoformat() if _universe_cache["fetched_at"] else None
    return jsonify({
        "count":      len(tickers),
        "source":     source,
        "fetched_at": fetched,
        "tickers":    sorted(tickers),
    })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shadow Log — paper-track every GO/WATCH setup, check outcome at +30min
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import csv, json as _json

SHADOW_LOG_PATH = os.path.join(os.path.dirname(__file__), "shadow_log.csv")
SHADOW_LOG_COLS = [
    "date", "scan_time", "ticker", "verdict", "priority", "score",
    "entry_price", "stop", "target1", "target2",
    "gap_pct", "rel_vol", "catalyst", "float_m", "short_float",
    "above_vwap", "spread_pct",
    "outcome_time", "outcome_price", "outcome_pct",
    "hit_target1", "hit_target2", "hit_stop", "result",
    "would_profit",
]

_shadow_pending = []   # list of dicts waiting for 30-min lookback
_shadow_lock    = threading.Lock()


def _ensure_shadow_log():
    """Create CSV with header if it doesn't exist."""
    if not os.path.exists(SHADOW_LOG_PATH):
        with open(SHADOW_LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=SHADOW_LOG_COLS).writeheader()


def _append_shadow_rows(rows):
    """Append completed rows to the CSV."""
    _ensure_shadow_log()
    with open(SHADOW_LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHADOW_LOG_COLS, extrasaction="ignore")
        for row in rows:
            w.writerow(row)


def _log_setups(results):
    """Called at scan time — record every GO/WATCH setup for later evaluation."""
    et = datetime.now(timezone(timedelta(hours=-5)))
    try:
        import pytz
        et = datetime.now(pytz.timezone("America/New_York"))
    except ImportError:
        pass

    logged = 0
    for r in results:
        if r.get("verdict") not in ("GO", "WATCH"):
            continue
        t = r.get("trade", {})
        entry = {
            "date":        et.strftime("%Y-%m-%d"),
            "scan_time":   et.strftime("%H:%M"),
            "ticker":      r["ticker"],
            "verdict":     r.get("verdict"),
            "priority":    r.get("score", {}).get("priority", "?"),
            "score":       r.get("score", {}).get("total", 0),
            "entry_price": r.get("price"),
            "stop":        t.get("stop"),
            "target1":     t.get("target1"),
            "target2":     t.get("target2"),
            "gap_pct":     r.get("gap"),
            "rel_vol":     r.get("relVol") or r.get("score", {}).get("relVol"),
            "catalyst":    r.get("catalyst"),
            "float_m":     r.get("float"),
            "short_float": r.get("shortFloat"),
            "above_vwap":  r.get("aboveVwap"),
            "spread_pct":  r.get("spreadPct"),
            # outcome fields filled in later
            "outcome_time":  None,
            "outcome_price": None,
            "outcome_pct":   None,
            "hit_target1":   None,
            "hit_target2":   None,
            "hit_stop":      None,
            "result":        None,
            "would_profit":  None,
            "_check_after":  et.timestamp() + 1800,   # 30 min from now
        }
        with _shadow_lock:
            _shadow_pending.append(entry)
        logged += 1

    if logged:
        print(f"  📓 Shadow log: queued {logged} setup(s) for 30-min lookback")


def _resolve_pending():
    """Check if any pending setups are due for outcome evaluation."""
    now_ts = datetime.now(timezone(timedelta(hours=-5))).timestamp()

    with _shadow_lock:
        due   = [e for e in _shadow_pending if e["_check_after"] <= now_ts]
        still = [e for e in _shadow_pending if e["_check_after"] >  now_ts]

    if not due:
        return

    completed = []
    for entry in due:
        ticker = entry["ticker"]
        try:
            # Fetch current price via Alpaca snapshot
            client = StockHistoricalDataClient(API_KEY, API_SECRET)
            snap   = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=[ticker]))
            s      = snap.get(ticker)
            if s and s.latest_trade:
                outcome_price = float(s.latest_trade.price)
            elif s and s.minute_bar:
                outcome_price = float(s.minute_bar.close)
            else:
                outcome_price = None
        except Exception as e:
            print(f"  Shadow lookback error {ticker}: {e}")
            outcome_price = None

        entry_price = entry.get("entry_price") or 0
        stop        = entry.get("stop")
        target1     = entry.get("target1")
        target2     = entry.get("target2")

        if outcome_price and entry_price:
            outcome_pct = round((outcome_price - entry_price) / entry_price * 100, 2)
            hit_t1  = target1 and outcome_price >= float(target1)
            hit_t2  = target2 and outcome_price >= float(target2)
            hit_stp = stop    and outcome_price <= float(stop)

            if hit_t2:      result = "TARGET2"
            elif hit_t1:    result = "TARGET1"
            elif hit_stp:   result = "STOPPED"
            elif outcome_pct > 0: result = "POSITIVE"
            else:           result = "NEGATIVE"

            # Simplified P&L: 1 unit risk, target = 2:1
            if result in ("TARGET1", "TARGET2"):
                would_profit = round(abs(entry_price - float(stop)) * (2 if hit_t2 else 1), 2) if stop else None
            elif result == "STOPPED":
                would_profit = -round(abs(entry_price - float(stop)), 2) if stop else None
            else:
                would_profit = round(outcome_price - entry_price, 2)
        else:
            outcome_pct  = None
            hit_t1 = hit_t2 = hit_stp = None
            result = "NO_DATA"
            would_profit = None

        et = datetime.now(timezone(timedelta(hours=-5)))
        entry.update({
            "outcome_time":  et.strftime("%H:%M"),
            "outcome_price": outcome_price,
            "outcome_pct":   outcome_pct,
            "hit_target1":   hit_t1,
            "hit_target2":   hit_t2,
            "hit_stop":      hit_stp,
            "result":        result,
            "would_profit":  would_profit,
        })
        completed.append(entry)
        print(f"  📓 Shadow result: {ticker} entry ${entry_price} → ${outcome_price} ({outcome_pct:+.2f}%) [{result}]")

    # Save completed rows
    _append_shadow_rows(completed)

    # Update pending list
    with _shadow_lock:
        _shadow_pending.clear()
        _shadow_pending.extend(still)


def _load_shadow_log():
    """Load all rows from CSV as list of dicts."""
    _ensure_shadow_log()
    try:
        with open(SHADOW_LOG_PATH, "r", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _shadow_stats(rows):
    """Compute win rate, avg gain, expectancy from completed rows."""
    done = [r for r in rows if r.get("result") and r["result"] != "NO_DATA"]
    if not done:
        return {"total": 0}

    wins    = [r for r in done if r["result"] in ("TARGET1", "TARGET2", "POSITIVE")]
    losses  = [r for r in done if r["result"] in ("STOPPED", "NEGATIVE")]
    profits = []
    for r in done:
        try:
            profits.append(float(r["would_profit"]) if r["would_profit"] else 0)
        except (ValueError, TypeError):
            pass

    by_verdict  = {}
    by_priority = {}
    for r in done:
        v = r.get("verdict", "?")
        p = r.get("priority", "?")
        by_verdict[v]  = by_verdict.get(v, {"w":0,"l":0})
        by_priority[p] = by_priority.get(p, {"w":0,"l":0})
        key = "w" if r["result"] in ("TARGET1","TARGET2","POSITIVE") else "l"
        by_verdict[v][key]  += 1
        by_priority[p][key] += 1

    avg_profit   = round(sum(profits) / len(profits), 3) if profits else 0
    total_profit = round(sum(profits), 2)
    win_rate     = round(len(wins) / len(done) * 100, 1) if done else 0

    return {
        "total":        len(done),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate_pct": win_rate,
        "avg_profit":   avg_profit,
        "total_profit": total_profit,
        "by_verdict":   by_verdict,
        "by_priority":  by_priority,
        "pending":      len(_shadow_pending),
    }


# ── Shadow log API endpoints ──────────────────────────────────────────────────

@app.route("/api/shadow-log")
def api_shadow_log():
    """Return all shadow log rows + stats summary."""
    rows  = _load_shadow_log()
    stats = _shadow_stats(rows)
    # Most recent 200 rows for UI
    recent = rows[-200:] if len(rows) > 200 else rows
    recent.reverse()
    return jsonify({"rows": recent, "stats": stats, "pending": len(_shadow_pending)})


@app.route("/api/shadow-log/clear", methods=["POST"])
def api_shadow_log_clear():
    """Wipe the shadow log (use carefully)."""
    try:
        _ensure_shadow_log()
        with open(SHADOW_LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=SHADOW_LOG_COLS).writeheader()
        with _shadow_lock:
            _shadow_pending.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    print("\n" + "═"*52)
    print("  GAP SCANNER — Alpaca + yfinance")
    print("═"*52)
    print(f"  Alpaca:    {'✅' if ALPACA_OK              else '❌  pip install alpaca-py'}")
    print(f"  Trading:   {'✅ PAPER' if TRADING_OK and IS_PAPER else ('✅ LIVE' if TRADING_OK else '❌ missing')}")
    if YF_OK:
        import yfinance as _yf
        _yv = tuple(int(x) for x in _yf.__version__.split(".")[:2])
        _yf_ok = _yv >= (1, 2)
        print(f"  yfinance:  {'✅' if _yf_ok else '⚠ '} v{_yf.__version__}{'  ← upgrade: pip install yfinance>=1.2.0' if not _yf_ok else ''}")
    else:
        print(f"  yfinance:  ❌  pip install yfinance")
    print(f"  API Keys:  {'✅' if API_KEY and API_SECRET  else '❌  check config.py'}")
    print(f"  Universe:  dynamic (top gainers + most active + {len(WATCHLIST_FALLBACK)} watchlist)")
    print(f"  Email:     {'✅ ' + EMAIL_TO if EMAIL_ENABLED else '❌  disabled (set EMAIL_ENABLED=True in config.py)'}")
    print(f"  Scheduler: {'✅ every ' + str(SCAN_INTERVAL_MIN) + 'min  ' + SCAN_WINDOW_START + '–' + SCAN_WINDOW_END + ' ET' if SCHEDULER_ENABLED else '❌  disabled (set SCHEDULER_ENABLED=True in config.py)'}")
    print(f"  Auto-trade:{'✅ GO+VWAP  max ' + str(AUTO_MAX_TRADES_PER_DAY) + '/day  PAPER' if AUTO_TRADE_ENABLED and IS_PAPER else ('⚠  LIVE — be careful!' if AUTO_TRADE_ENABLED and not IS_PAPER else '❌  disabled (set AUTO_TRADE_ENABLED=True)')}")
    print(f"  Server:    http://localhost:5001")
    print(f"  Debug:     http://localhost:5001/api/diagnostic")
    print("═"*52 + "\n")
    if SCHEDULER_ENABLED:
        _start_scheduler()
    if AUTO_TRADE_ENABLED and TRADING_OK:
        _start_order_monitor()
    app.run(host="0.0.0.0", port=5001, debug=False)
