@echo off
cd /d "%~dp0"
echo === Renderer Worker Setup ===

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing Python dependencies...
pip install -q -r requirements.txt

echo Installing Playwright Chromium...
playwright install chromium

echo.
echo === Starting renderer on http://localhost:7777 ===
echo Set RENDERER_URL=http://localhost:7777 in Replit Secrets
echo.
python app.py
pause
