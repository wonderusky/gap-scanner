#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  start.sh — Gap Scanner launcher
#  Run this every morning instead of manual commands
#
#  First time: chmod +x start.sh
#  Every time: ./start.sh
# ─────────────────────────────────────────────────────────────

# Get the directory this script lives in
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo ""
echo "═══════════════════════════════════════"
echo "   GAP SCANNER — Starting Up"
echo "═══════════════════════════════════════"

# ── Step 1: Create venv if it doesn't exist ──────────────────
if [ ! -d "venv" ]; then
  echo "→ Creating virtual environment..."
  python3 -m venv venv
  if [ $? -ne 0 ]; then
    echo "✗ Failed to create venv. Is Python 3 installed?"
    echo "  Run: brew install python3"
    exit 1
  fi
  echo "✓ Virtual environment created"
fi

# ── Step 2: Activate venv ────────────────────────────────────
echo "→ Activating virtual environment..."
source venv/bin/activate

# ── Step 3: Install/update dependencies ──────────────────────
echo "→ Checking dependencies..."
pip install -q alpaca-py flask flask-cors

if [ $? -ne 0 ]; then
  echo "✗ Failed to install packages"
  exit 1
fi
echo "✓ Dependencies ready"

# ── Step 4: Check config.py has real keys ────────────────────
if grep -q "YOUR_ALPACA_API_KEY_HERE" config.py 2>/dev/null; then
  echo ""
  echo "⚠  WARNING: Alpaca keys not set in config.py"
  echo "   Open config.py and add your keys from:"
  echo "   https://app.alpaca.markets → API Keys"
  echo ""
fi

# ── Step 5: Open scanner in browser (after short delay) ──────
echo "→ Opening scanner in browser in 2 seconds..."
(sleep 2 && open scanner.html) &

# ── Step 6: Start server ─────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo "   Server: http://localhost:5001"
echo "   Press Ctrl+C to stop"
echo "═══════════════════════════════════════"
echo ""

python server.py

# Deactivate venv when server stops
deactivate
