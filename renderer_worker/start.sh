#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "=== Renderer Worker Setup ==="

# Create + activate venv if missing
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing Python dependencies..."
pip install -q -r requirements.txt

echo "Installing Playwright Chromium..."
playwright install chromium

echo ""
echo "=== Starting renderer on http://localhost:7777 ==="
echo "Set RENDERER_URL=http://localhost:7777 in Replit Secrets"
echo ""
python app.py
