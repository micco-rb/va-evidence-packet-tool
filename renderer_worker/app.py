"""
Renderer Worker — Playwright/Chromium PDF service.

Endpoints:
  GET  /health   → {"ok": true, "chromium": "<version>"}
  POST /render   → JSON {"url": "..."} → PDF bytes (application/pdf)

Each /render request opens a fresh browser context with full desktop-Chrome
fingerprinting to avoid anti-bot 403 responses on PubMed / publisher pages.
Per-request context ensures thread safety (no greenlet cross-thread errors).
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


# ── Chromium launch args ───────────────────────────────────────────────────────
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

# ── Realistic desktop-Chrome browser context ──────────────────────────────────
# Matches Chrome 124 on Windows 10 (common, well-trusted UA string).
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CONTEXT_OPTS = dict(
    user_agent  = _UA,
    viewport    = {"width": 1365, "height": 900},
    locale      = "en-US",
    timezone_id = "America/Chicago",
    # Headers sent with every navigation request
    extra_http_headers = {
        "Accept":                    (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language":           "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        # Client-hint headers expected from Chrome 124 on Windows
        "sec-ch-ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile":          "?0",
        "sec-ch-ua-platform":        '"Windows"',
        # Sec-Fetch headers that Chrome sends on a top-level navigation
        "sec-fetch-dest":            "document",
        "sec-fetch-mode":            "navigate",
        "sec-fetch-site":            "none",
        "sec-fetch-user":            "?1",
    },
)

# ── Init script injected into every page ──────────────────────────────────────
# Masks automation signals that bot-detection scripts look for.
_INIT_SCRIPT = """
    // 1. navigator.webdriver → undefined (primary bot signal)
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Spoof plugins array so it looks non-empty
    Object.defineProperty(navigator, 'plugins', {
        get: () => Object.assign([1,2,3,4,5], { item: (i) => i }),
    });

    // 3. Realistic language list
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    // 4. Remove CDP / DevTools automation globals left by Chromium
    const _cdcKeys = Object.keys(window).filter(k => k.startsWith('cdc_'));
    _cdcKeys.forEach(k => { try { delete window[k]; } catch(_) {} });
"""

# ── PDF print options ──────────────────────────────────────────────────────────
_PDF_OPTS = dict(
    format               = "Letter",
    print_background     = True,
    prefer_css_page_size = True,
    margin               = dict(
        top    = "0.9in",
        left   = "0.4in",
        right  = "0.42in",
        bottom = "0.39in",
    ),
)

# ── Interstitial / browser-check phrases ──────────────────────────────────────
_INTERSTITIAL_PHRASES = [
    "checking your browser",
    "automatically redirected",
    "verifying you are human",
    "just a moment",
    "please wait",
    "ddos protection",
    "ray id",
    "enable javascript",
    "browser verification",
]

# Selectors that are present on real article / abstract pages but never on
# interstitial pages.  Checked in order — first match wins.
_ARTICLE_SELECTORS = [
    "#article-details",          # PubMed article wrapper
    ".abstract-content",         # PubMed abstract body
    "#abstract",                 # common abstract id
    ".article-page",
    ".full-view",
    "article",                   # HTML5 semantic element
    ".article",
    ".content-main",
    "main",                      # broad fallback
]

# Returns true once ALL interstitial phrases have left the body text.
_INTERSTITIAL_CLEAR_JS = """
(phrases) => {
    const txt = document.body ? document.body.innerText.toLowerCase() : '';
    return !phrases.some(p => txt.includes(p));
}
"""


def _wait_for_article(page, timeout_ms: int = 12_000) -> bool:
    """
    Poll article-content selectors until one becomes visible.
    Returns True if a selector matched, False if none appeared within the timeout.
    """
    per_sel_ms = max(1_500, timeout_ms // len(_ARTICLE_SELECTORS))
    for sel in _ARTICLE_SELECTORS:
        try:
            page.wait_for_selector(sel, state="visible", timeout=per_sel_ms)
            print(f"  [pw] article content visible: {sel!r}  url={page.url!r}",
                  flush=True)
            return True
        except Exception:
            continue
    print(f"  [pw] no article selector matched  url={page.url!r}", flush=True)
    return False


def _wait_past_interstitial(page, interstitial_timeout_ms: int = 45_000) -> None:
    """
    Called after the initial goto().

    Flow:
      1. Read body text. If no interstitial phrase is found, jump straight to
         article-content waiting and return.
      2. If an interstitial IS found, call wait_for_function(phrases_clear).
         • Happy path  — phrases disappear without a navigation:
             → wait_for_load_state(networkidle) + article selectors + settle.
         • Navigation path — Cloudflare's JS redirect fires a page navigation
             which causes wait_for_function to throw.  This is NOT an error;
             it means the challenge passed and we are mid-redirect.
             → wait_for_load_state(networkidle) on the NEW page, then article
               selectors + settle.  Do NOT return early.
    """
    # ── Step 1: detect ────────────────────────────────────────────────────
    try:
        body_text = page.inner_text("body", timeout=5_000).lower()
    except Exception:
        return

    found = [p for p in _INTERSTITIAL_PHRASES if p in body_text]

    if not found:
        # No interstitial — still wait for article content to paint
        _wait_for_article(page, timeout_ms=8_000)
        return

    print(f"  [pw] interstitial detected: {found}", flush=True)
    print(f"  [pw] waiting up to {interstitial_timeout_ms // 1000}s …", flush=True)

    # ── Step 2: wait for challenge to clear ───────────────────────────────
    navigation_fired = False
    try:
        page.wait_for_function(
            _INTERSTITIAL_CLEAR_JS,
            arg     = _INTERSTITIAL_PHRASES,
            timeout = interstitial_timeout_ms,
            polling = 500,
        )
        print(f"  [pw] interstitial cleared (no navigation)  url={page.url!r}",
              flush=True)
    except Exception as exc:
        # Most likely cause: Cloudflare's JS redirect triggered a navigation,
        # which invalidated the wait_for_function frame context.
        # Treat this as "redirect in progress" — do NOT return here.
        err_str = str(exc).lower()
        if any(k in err_str for k in ("navigat", "detach", "target closed",
                                       "execution context")):
            navigation_fired = True
            print(f"  [pw] redirect/navigation detected during wait — "
                  f"waiting for new page to load …", flush=True)
        else:
            # Genuine timeout: challenge never passed.  Proceed with whatever
            # is on screen (could still be the interstitial, but nothing more
            # we can do).
            print(f"  [pw] challenge wait timed out ({exc.__class__.__name__}) "
                  f"— proceeding", flush=True)

    # ── Step 3: wait for the (possibly new) page to reach network idle ────
    try:
        page.wait_for_load_state("networkidle", timeout=25_000)
        print(f"  [pw] networkidle  url={page.url!r}", flush=True)
    except Exception as exc:
        print(f"  [pw] networkidle wait: {exc.__class__.__name__}", flush=True)

    # ── Step 4: wait for real article content to appear ───────────────────
    _wait_for_article(page, timeout_ms=12_000)

    # Final settle for JS-painted content (abstracts rendered after hydration)
    page.wait_for_timeout(1_500)


# ── Core render function ───────────────────────────────────────────────────────

def _render_pdf(url: str, timeout: int = 60_000) -> bytes:
    """
    Open url in a realistic desktop-Chrome context, call page.pdf().

    No paywall detection.  No content filtering.
    Returns raw PDF bytes.  Raises on any Playwright error.
    """
    print(f"  [pw] launching chromium", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        print(f"  [pw] browser launched  version={browser.version}", flush=True)

        # Fresh context with full desktop-Chrome fingerprint
        context = browser.new_context(**_CONTEXT_OPTS)
        context.add_init_script(_INIT_SCRIPT)
        page = context.new_page()

        try:
            # Short human-like pause before navigation
            page.wait_for_timeout(400)

            # ── Step 1: navigate ───────────────────────────────────────────
            print(f"  [pw] goto {url!r}  timeout={timeout}", flush=True)
            try:
                nav = page.goto(url, wait_until="networkidle", timeout=timeout)
                print(f"  [pw] networkidle  http={nav.status if nav else '?'}", flush=True)
            except Exception as e:
                print(f"  [pw] networkidle failed ({e}) — retrying domcontentloaded",
                      flush=True)
                nav = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                print(f"  [pw] domcontentloaded  http={nav.status if nav else '?'}",
                      flush=True)

            # ── Step 2: interstitial / redirect guard ──────────────────────
            _wait_past_interstitial(page)

            # ── Step 3: hard article validation (attempt 1) ────────────────
            print(f"  [pw] validating article content (attempt 1) …", flush=True)
            article_ok = _wait_for_article(page, timeout_ms=12_000)

            # ── Step 4: retry if article not found ─────────────────────────
            if not article_ok:
                print(f"  [pw] article not found — retrying full navigation …",
                      flush=True)
                try:
                    page.wait_for_timeout(1_500)
                    nav2 = page.goto(url, wait_until="domcontentloaded",
                                     timeout=timeout)
                    print(f"  [pw] retry http={nav2.status if nav2 else '?'}",
                          flush=True)
                    _wait_past_interstitial(page)
                    article_ok = _wait_for_article(page, timeout_ms=15_000)
                except Exception as exc:
                    print(f"  [pw] retry navigation failed: {exc}", flush=True)

            # ── Step 5: refuse to print interstitial page ──────────────────
            if not article_ok:
                raise ValueError(
                    f"Article content not found after retry for {url!r} — "
                    "interstitial not cleared. PMID will be skipped."
                )

            # ── Step 6: mandatory 3 s settle before printing ───────────────
            print(f"  [pw] article validated — settling 3 s …", flush=True)
            page.wait_for_timeout(3_000)

            # ── Step 7: print to PDF ───────────────────────────────────────
            print(f"  [pw] calling page.pdf() …", flush=True)
            pdf = page.pdf(**_PDF_OPTS)
            valid = pdf[:4] == b"%PDF" if pdf else False
            print(f"  [pw] pdf() → {len(pdf):,} bytes  valid={valid}", flush=True)
            return pdf

        finally:
            page.close()
            context.close()
            browser.close()
            print(f"  [pw] browser closed", flush=True)


# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/health")
def health():
    """Liveness probe — confirms Playwright + Chromium are functional."""
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

    print(f"\n[render] ── START ────────────────────────────────", flush=True)
    print(f"[render] url={url!r}", flush=True)

    try:
        pdf_bytes = _render_pdf(url)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[render] EXCEPTION:\n{tb}", flush=True)
        return jsonify(error=str(exc), traceback=tb), 500

    print(f"[render] bytes={len(pdf_bytes):,}  first8={pdf_bytes[:8]!r}", flush=True)

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        msg = f"page.pdf() returned {len(pdf_bytes)} bytes — not a valid PDF"
        print(f"[render] FAIL: {msg}", flush=True)
        return jsonify(error=msg), 502

    print(f"[render] SUCCESS ────────────────────────────────────\n", flush=True)
    return Response(
        pdf_bytes,
        mimetype = "application/pdf",
        headers  = {"X-PDF-Size": str(len(pdf_bytes))},
    )


# ── Startup ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = 7777
    print(f"\n  Renderer Worker — http://localhost:{port}")
    print(f"  Set in Replit Secrets:  RENDERER_URL = https://<railway-domain>\n")
    # threaded=True is safe: each request owns its own Playwright context.
    app.run(host="0.0.0.0", port=port, threaded=True)
