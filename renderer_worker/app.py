"""
Renderer Worker — Playwright/Chromium PDF service.

Runs on your local machine (outside Replit).

Endpoints:
  GET  /health   → {"ok": true}
  POST /render   → JSON {"url": "..."} → PDF bytes (application/pdf)

The browser is launched ONCE at startup and reused for all requests.
This avoids the overhead of spawning Chromium for every URL.
"""

import asyncio
import sys
import threading

from flask import Flask, request, Response, jsonify

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed.")
    print("Run:  pip install playwright && playwright install chromium")
    sys.exit(1)

# ── Browser singleton ─────────────────────────────────────────────────────────
_pw       = None
_browser  = None
_lock     = threading.Lock()

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--disable-default-apps",
    "--mute-audio",
    "--hide-scrollbars",
    "--no-first-run",
    "--disable-popup-blocking",
]

_PDF_OPTS = dict(
    format           = "Letter",
    print_background = True,
    margin           = dict(top="0.5in", bottom="0.5in",
                            left="0.65in", right="0.65in"),
)


def _get_browser():
    """Return the shared browser instance, launching it if needed."""
    global _pw, _browser
    if _browser is None or not _browser.is_connected():
        if _pw is not None:
            try:
                _pw.stop()
            except Exception:
                pass
        _pw      = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        print(f"[worker] Browser launched — {_browser.version}", flush=True)
    return _browser


def _render_pdf(url: str, timeout: int = 45_000) -> bytes:
    """Open url in a new page, print to PDF, close the page."""
    browser = _get_browser()
    page    = browser.new_page()
    try:
        print(f"  → {url}", flush=True)
        page.goto(url, wait_until="networkidle", timeout=timeout)
        page.wait_for_timeout(2000)          # let JS/CSS settle
        pdf = page.pdf(**_PDF_OPTS)
        print(f"  ✓ {len(pdf):,} bytes", flush=True)
        return pdf
    finally:
        page.close()


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/health")
def health():
    with _lock:
        try:
            browser = _get_browser()
            ver     = browser.version
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 503
    return jsonify(ok=True, service="renderer-worker", version="1.0",
                   chromium=ver)


@app.post("/render")
def render():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="url is required"), 400

    with _lock:                              # one render at a time
        try:
            pdf_bytes = _render_pdf(url)
        except Exception as exc:
            print(f"  [error] {exc}", flush=True)
            return jsonify(error=str(exc)), 500

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        return jsonify(error="Chromium did not return a valid PDF"), 502

    return Response(
        pdf_bytes,
        mimetype = "application/pdf",
        headers  = {"X-PDF-Size": str(len(pdf_bytes))},
    )


# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = 7777
    print(f"\n  Renderer Worker — http://localhost:{port}")
    print(f"  Set in Replit Secrets:  RENDERER_URL = http://<your-LAN-ip>:{port}\n")

    # Pre-launch the browser so the first request isn't slow
    with _lock:
        _get_browser()

    # threaded=False: all requests run in the same (main) thread where the
    # Playwright browser was launched.  With threaded=True Flask spawns a new
    # thread per request and Playwright's sync API (which uses greenlets
    # internally) raises "Cannot switch to a different thread".
    app.run(host="0.0.0.0", port=port, threaded=False)
