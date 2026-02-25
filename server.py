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
except ImportError:
    API_KEY    = os.environ.get("ALPACA_API_KEY", "")
    API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
    IS_PAPER   = True

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

# ── Universe — broad watchlist of historically active gappers ─────────────────
WATCHLIST = [
    # Mega-cap tech
    "NVDA","AMD","TSLA","META","AMZN","GOOGL","MSFT","AAPL","NFLX","CRM","ORCL","ADBE",
    # High-vol momentum
    "SMCI","HIMS","APP","PLTR","HOOD","SOFI","UPST","MSTR","IONQ","RKLB","ACHR","JOBY",
    # Biotech / Healthcare
    "AXSM","WGS","TNDM","OCGN","RXST","BBIO","AGIO","NVAX","SAVA","ACAD","SRPT",
    "RARE","EDIT","NTLA","BEAM","CRSP","FOLD","CELC","IMVT","LEGN","KYMR",
    # Small/mid momentum
    "LUNR","LAZR","ARRY","SOUN","MARA","RIOT","CLSK","HUT","CIFR","BTBT","WULF","IREN",
    # Semis
    "CRDO","WOLF","CRUS","MRVL","AEHR","COHU","AMBA","LSCC","RMBS",
    # Consumer / fintech
    "DASH","LYFT","UBER","ABNB","DKNG","PENN","AFRM","CHWY","ETSY","PINS","OPEN",
    # EV / Energy
    "CHPT","BLNK","EVGO","RIVN","LCID","NIO","XPEV","LI",
]



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
      4. Default to "news"
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

    # 4. Default
    return "news", "default", "No specific catalyst identified"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# yfinance helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fetch_one_yf(ticker):
    """Pull fundamentals for a single ticker. Returns (ticker, dict)."""
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        float_shares = info.get("floatShares") or info.get("sharesOutstanding") or 0
        float_m      = round(float_shares / 1_000_000, 1) if float_shares else 150.0
        avg_vol      = info.get("averageVolume10days") or info.get("averageVolume") or 0
        sector       = info.get("sector") or "—"

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

        return ticker, {
            "float":        float_m,
            "sector":       sector,
            "avg_vol":      avg_vol,
            "has_earnings": has_earnings,
            "short_float":  short_float,
        }
    except Exception as e:
        return ticker, {"float": 150.0, "sector": "—", "avg_vol": 0,
                        "has_earnings": False, "short_float": 0.0}


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
    # Relative volume (35%)
    if rel_vol < 1:    vs = 0
    elif rel_vol < 2:  vs = 30
    elif rel_vol < 3:  vs = 60
    elif rel_vol < 5:  vs = 85
    else:              vs = 100

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

    # Gap sweet spot 4–20% (15%)
    if gap_pct < 3:        gs = 20
    elif gap_pct < 4:      gs = 50
    elif gap_pct <= 20:    gs = min(100, gap_pct * 5)
    elif gap_pct <= 25:    gs = 80
    else:                  gs = 55

    total = round(vs * 0.35 + cs * 0.30 + fs * 0.20 + gs * 0.15)

    checks = {
        "relVol":      rel_vol >= 2,
        "catalyst":    catalyst in ("earnings", "fda"),
        "float":       float_m < 100,
        "gap":         4 <= gap_pct <= 20,
        "gapExtended": gap_pct > 25,
    }
    passes = sum([checks["relVol"], checks["catalyst"], checks["float"], checks["gap"]])

    if checks["gapExtended"]:  priority = "C"
    elif passes == 4:          priority = "A"
    elif passes == 3:          priority = "B"
    elif passes == 2:          priority = "C"
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
    client    = StockHistoricalDataClient(API_KEY, API_SECRET)
    req       = StockSnapshotRequest(symbol_or_symbols=WATCHLIST)
    snapshots = client.get_stock_snapshot(req)
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
        short_float = yfd.get("short_float", 0.0)

        # ── Trade verdict ──────────────────────────────────────────────────────
        # GO:   A/B rank + above VWAP + rel_vol ≥2x + tight spread
        # WATCH: B/C rank or one condition missing
        # SKIP:  C rank or rel_vol <1x or very wide spread
        s        = score_setup(c["gap"], rel_vol, float_m, catalyst)
        rank_val = s.get("rank", "C")
        go_conditions = [
            rank_val in ("A", "B"),
            above_vwap is True,
            rel_vol >= 2.0,
            spread_pct is not None and spread_pct < 0.5,
            float_m < 100,
        ]
        go_count = sum(go_conditions)
        if rank_val == "A" and go_count >= 4:
            verdict = "GO"
        elif rank_val in ("A","B") and go_count >= 3:
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
            "shortFloat":  short_float,
            "verdict":     verdict,
            "catalyst":    catalyst,
            "catSource":   cat_source,
            "catDetail":   cat_detail,
            "secFiling":   {"title": sec_filing["title"][:80], "url": sec_filing["url"],
                            "filed_at": sec_filing["filed_at"]} if sec_filing else None,
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
            "earnings_today": earnings_today, "skipped": skipped}
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


@app.route("/api/test-technicals", methods=["GET"])
def api_test_technicals():
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
        return jsonify({
            "ok":       True,
            "order_id": str(order.id),
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
    print(f"  Universe:  {len(WATCHLIST)} tickers")
    print(f"  Server:    http://localhost:5001")
    print(f"  Debug:     http://localhost:5001/api/diagnostic")
    print("═"*52 + "\n")
    app.run(host="0.0.0.0", port=5001, debug=False)
