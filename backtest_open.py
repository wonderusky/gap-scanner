"""
backtest_open.py — Backtest shadow log setups using real 9:30 open bar data via yfinance

Strategy:
  - One trade per day: best GO setup by score, pre-market scan only
  - Entry = actual 9:30 open price
  - Stop = max(pre-market stop distance, 0.8% of open price)
  - Target1 = 1R, Target2 = 2R
  - Exit at 45 min (10:15) if no target/stop hit
  - Risk = 0.5% of account per trade

Run: python3 backtest_open.py
"""
import os, csv, time, sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import warnings
warnings.filterwarnings("ignore")

SHADOW_CSV    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shadow_log.csv")
OUTPUT_CSV    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results.csv")
ACCOUNT_SIZE  = 100_000
RISK_PCT      = 0.005      # $500 risk per trade
POSITION_PCT  = 0.25       # max 25% account = $25k
STOP_BUFFER   = 0.008      # min stop = 0.8% below open
MAX_HOLD_MINS = 45
ONE_TRADE_PER_DAY = True

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance"); sys.exit(1)

# Load shadow log
rows = list(csv.DictReader(open(SHADOW_CSV)))
print(f"Shadow log: {len(rows)} rows")

def is_premarket(t):
    try:
        h,m = map(int,t.split(":")); return h<9 or (h==9 and m<30)
    except: return False

# Group pre-market GO/WATCH setups by date
by_date = defaultdict(list)
for r in rows:
    if not is_premarket(r.get("scan_time","")): continue
    if r.get("verdict") not in ("GO","WATCH"): continue
    try:
        if float(r.get("entry_price",0)) == 0: continue
    except: continue
    by_date[r["date"]].append(r)

print(f"Pre-market setups: {sum(len(v) for v in by_date.values())} across {len(by_date)} days\n")

# Cache minute bars per ticker per day
_cache = {}
def get_bars(ticker, trade_date):
    key = (ticker, trade_date)
    if key in _cache: return _cache[key]
    try:
        d = datetime.strptime(trade_date, "%Y-%m-%d")
        end = (d + timedelta(days=1)).strftime("%Y-%m-%d")
        hist = yf.Ticker(ticker).history(
            start=trade_date, end=end,
            interval="1m", prepost=False
        )
        _cache[key] = hist
        time.sleep(0.3)
        return hist
    except Exception as e:
        print(f"  yf err {ticker}: {e}")
        _cache[key] = None
        return None

def simulate(bars, open_price, stop, t1, t2, max_mins=MAX_HOLD_MINS):
    """Walk minute bars. Returns (outcome, exit_price, mins_held)"""
    if bars is None or bars.empty: return "NO_DATA", open_price, 0
    first = True
    mins  = 0
    for ts, row in bars.iterrows():
        # Only market hours bars (9:30 onward)
        h = ts.hour; m = ts.minute
        if h < 9 or (h == 9 and m < 30): continue
        if first: first = False; continue   # skip entry bar
        mins += 1
        lo, hi = float(row["Low"]), float(row["High"])
        if lo <= stop:              return "STOPPED",  stop,       mins
        if t2 and hi >= t2:        return "TARGET2",  t2,         mins
        if hi >= t1:               return "TARGET1",  t1,         mins
        if mins >= max_mins:       return "TIMEOUT",  float(row["Close"]), mins
    return "NO_CLOSE", float(bars.iloc[-1]["Close"]) if not bars.empty else open_price, mins

# ── Run ───────────────────────────────────────────────────────────────────────
all_results = []
daily_pnl   = {}

print(f"{'='*60}")
print(f"Running backtest: 1 trade/day, best GO setup, 0.5% risk")
print(f"{'='*60}\n")

for trade_date in sorted(by_date.keys()):
    setups = by_date[trade_date]
    # Best: GO first, then highest score
    setups.sort(key=lambda r: (0 if r.get("verdict")=="GO" else 1, -float(r.get("score",0))))
    candidates = setups[:1] if ONE_TRADE_PER_DAY else setups
    day_pnl = 0

    for r in candidates:
        ticker   = r["ticker"]
        pm_price = float(r["entry_price"])
        pm_stop  = float(r.get("stop") or 0)

        bars = get_bars(ticker, trade_date)

        # Get 9:30 open price
        open_price = None
        if bars is not None and not bars.empty:
            for ts, row in bars.iterrows():
                if ts.hour == 9 and ts.minute == 30:
                    open_price = float(row["Open"])
                    break
            if open_price is None:
                # Try first bar of market hours
                mkt = bars[(bars.index.hour >= 9) & ~((bars.index.hour == 9) & (bars.index.minute < 30))]
                if not mkt.empty:
                    open_price = float(mkt.iloc[0]["Open"])

        if not open_price:
            print(f"  -- {trade_date} {ticker}: no 9:30 bar")
            all_results.append({**r, "bt_outcome":"NO_DATA", "bt_pnl_$":None})
            continue

        # Recalculate stops from actual open
        pm_dist   = (pm_price - pm_stop) if pm_stop else pm_price * 0.01
        stop_dist = max(pm_dist, open_price * STOP_BUFFER)
        stop = round(open_price - stop_dist, 2)
        t1   = round(open_price + stop_dist * 1.0, 2)
        t2   = round(open_price + stop_dist * 2.0, 2)

        risk_usd = ACCOUNT_SIZE * RISK_PCT
        shares   = int(risk_usd / stop_dist)
        shares   = min(shares, int(ACCOUNT_SIZE * POSITION_PCT / open_price))
        if shares < 1:
            print(f"  -- {trade_date} {ticker}: 0 shares (stop too tight)")
            continue

        outcome, exit_px, mins = simulate(bars, open_price, stop, t1, t2)
        pnl   = round((exit_px - open_price) * shares, 2)
        pnl_r = round((exit_px - open_price) / stop_dist, 2) if stop_dist else 0
        day_pnl += pnl

        icon = "+" if pnl > 0 else "-"
        stop_pct = stop_dist/open_price*100
        print(f"  [{icon}] {trade_date} {ticker:6}  pm={pm_price:.2f} open={open_price:.2f}  "
              f"stop={stop:.2f}({stop_pct:.1f}%)  t1={t1:.2f}  "
              f"[{outcome:8}] exit={exit_px:.2f}  ${pnl:+.0f} ({pnl_r:+.1f}R) {mins}min")

        all_results.append({
            **r,
            "bt_open":     open_price,
            "bt_stop":     stop,
            "bt_stop_pct": round(stop_pct, 2),
            "bt_t1":       t1,
            "bt_t2":       t2,
            "bt_outcome":  outcome,
            "bt_exit":     exit_px,
            "bt_pnl_r":    pnl_r,
            "bt_pnl_$":    pnl,
            "bt_shares":   shares,
            "bt_mins":     mins,
        })

    daily_pnl[trade_date] = day_pnl

# Save
if all_results:
    with open(OUTPUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_results[0].keys()))
        w.writeheader(); w.writerows(all_results)
    print(f"\nSaved: {OUTPUT_CSV}")

# Summary
print(f"\n{'='*60}\nBACKTEST SUMMARY\n{'='*60}")
bt = [r for r in all_results if r.get("bt_pnl_$") is not None]
if bt:
    total  = sum(float(r["bt_pnl_$"]) for r in bt)
    wins   = [r for r in bt if float(r["bt_pnl_$"]) > 0]
    losses = [r for r in bt if float(r["bt_pnl_$"]) <= 0]
    wr     = len(wins)/len(bt)*100
    print(f"Trades:      {len(bt)}  ({len(wins)}W / {len(losses)}L)  Win rate: {wr:.1f}%")
    print(f"Total P&L:   ${total:+,.0f}")
    print(f"Per trade:   ${total/len(bt):+.0f}")
    if wins:   print(f"Avg winner:  ${sum(float(r['bt_pnl_$']) for r in wins)/len(wins):+.0f}")
    if losses: print(f"Avg loser:   ${sum(float(r['bt_pnl_$']) for r in losses)/len(losses):+.0f}")
    print(f"\nOutcomes:")
    for o,c in Counter(r["bt_outcome"] for r in bt).most_common():
        print(f"  {o:12} {c:3}x  ({c/len(bt)*100:.0f}%)")
    print(f"\nDaily P&L:")
    running = 0
    for d in sorted(daily_pnl):
        v = daily_pnl[d]; running += v
        bar = ("+" if v>=0 else "-") * max(1, int(abs(v)/25))
        print(f"  {d}  ${v:+7.0f}   cum ${running:+,.0f}  {bar}")
    print(f"\n>>> {'PROFITABLE ✅' if total>0 else 'NOT PROFITABLE ❌'}")
    if total <= 0:
        print("\nWhat needs to change:")
        print("  1. Only trade tickers with open > pre-market price (gap holding)")
        print("  2. Require float < 50M (small floats move cleanly at open)")
        print("  3. Minimum rel vol 2x at scan time, not 1x")
        print("  4. Only GO verdict — remove WATCH from auto-trade")
        print("  5. Cut losers faster: exit if down 0.5R within first 5 bars")
else:
    print("No trades to summarize.")
