"""
Renderer Worker — Playwright/Chromium PDF service.

Endpoints:
  GET  /health   → {"ok": true, "chromium": "<version>"}
  POST /render   → JSON {"url": "..."} → PDF bytes (application/pdf)

Each /render request launches its own Playwright context and browser.
This is slower than a singleton but completely thread-safe — no greenlet
"Cannot switch to a different thread" errors regardless of Flask threading.
"""

import sys
import traceback

from flask import Flask, request, Response, jsonify

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed.")
    print("Run:  pip install playwright && playwright install chromium")
    sys.exit(1)

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


def _render_pdf(url: str, timeout: int = 60_000) -> bytes:
    """
    Open url in a brand-new Playwright context, call page.pdf(), return bytes.

    NO paywall detection.
    NO publisher checks.
    NO content filtering.

    Only failure modes:
      • Playwright throws during goto / pdf()  → exception propagates
      • page.pdf() returns bytes that don't start with %PDF → caller checks
    """
    print(f"  [pw] launching chromium for {url!r}", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        print(f"  [pw] browser launched  version={browser.version}", flush=True)

        page = browser.new_page()
        try:
            # ── Step 1: navigate ───────────────────────────────────────────
            print(f"  [pw] goto url={url!r}  wait_until=networkidle  timeout={timeout}", flush=True)
            try:
                nav_resp = page.goto(url, wait_until="networkidle", timeout=timeout)
                status   = nav_resp.status if nav_resp else "unknown"
                print(f"  [pw] page loaded  http_status={status}", flush=True)
            except Exception as nav_exc:
                # networkidle sometimes times-out on heavy pages; fall back to
                # domcontentloaded which is enough for a print-to-PDF.
                print(f"  [pw] networkidle failed ({nav_exc}) — retrying with domcontentloaded", flush=True)
                nav_resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                status   = nav_resp.status if nav_resp else "unknown"
                print(f"  [pw] page loaded (fallback)  http_status={status}", flush=True)

            # ── Step 2: settle ─────────────────────────────────────────────
            print(f"  [pw] waiting 3 s for JS/CSS to settle …", flush=True)
            page.wait_for_timeout(3_000)

            # ── Step 3: print to PDF ───────────────────────────────────────
            print(f"  [pw] calling page.pdf() …", flush=True)
            pdf = page.pdf(**_PDF_OPTS)
            valid = pdf[:4] == b"%PDF" if pdf else False
            print(f"  [pw] page.pdf() returned {len(pdf):,} bytes  valid_pdf={valid}", flush=True)
            return pdf

        finally:
            page.close()
            browser.close()
            print(f"  [pw] browser closed", flush=True)


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/health")
def health():
    """Lightweight liveness probe — just confirm Playwright can launch."""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            ver = browser.version
            browser.close()
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 503
    return jsonify(ok=True, service="renderer-worker", version="1.0", chromium=ver)


@app.post("/render")
def render():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="url is required"), 400

    print(f"\n[render] ── START ──────────────────────────────────────────", flush=True)
    print(f"[render] url={url!r}", flush=True)

    # ── Render ────────────────────────────────────────────────────────────
    try:
        pdf_bytes = _render_pdf(url)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[render] EXCEPTION during _render_pdf:\n{tb}", flush=True)
        return jsonify(error=str(exc), traceback=tb), 500

    # ── Validate ──────────────────────────────────────────────────────────
    print(f"[render] bytes received: {len(pdf_bytes):,}", flush=True)
    print(f"[render] first 8 bytes:  {pdf_bytes[:8]!r}", flush=True)

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        msg = f"page.pdf() returned {len(pdf_bytes)} bytes — not a valid PDF"
        print(f"[render] FAIL: {msg}", flush=True)
        return jsonify(error=msg), 502

    print(f"[render] SUCCESS — returning {len(pdf_bytes):,} bytes", flush=True)
    print(f"[render] ── END ────────────────────────────────────────────\n", flush=True)

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
    # threaded=True is fine now: each request creates its own Playwright context
    # so there is no shared browser state that could cause greenlet thread errors.
    app.run(host="0.0.0.0", port=port, threaded=True)
