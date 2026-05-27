"""
Renderer Worker — Playwright/Chromium PDF service.

Endpoints:
  GET  /health   → {"ok": true, "chromium": "<version>"}
  POST /render   → JSON {"url": "..."} → PDF bytes (application/pdf)

Each /render request opens a fresh browser context with full desktop-Chrome
fingerprinting to avoid anti-bot 403 responses on PubMed / publisher pages.
Per-request context ensures thread safety (no greenlet cross-thread errors).
"""

import random
import re
import sys
import time
import traceback
from pathlib import Path

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
        bottom = "0.25in",
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

# Selectors used by _wait_for_article (soft wait only — never rejects).
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

# ── Liberal acceptance check (used for hard validation in _one_attempt) ────────
# Accept if ANY selector exists OR body text contains any phrase.
_ACCEPT_SELECTORS = [
    "#article-details",
    "main",
    "article",
    ".heading-title",
    ".abstract-content",
    "#abstract",
]
_ACCEPT_TEXT_PHRASES = ["abstract", "pubmed"]

# ONLY reject as "hard blocked" when one of these phrases appears in body text.
_BLOCK_PHRASES = [
    "checking your browser",
    "cf-browser-verification",
    "access denied",
    "403 forbidden",
]


def _validate_article_page(page, body_lower: str = "") -> tuple[bool, str]:
    """
    Liberal acceptance: returns (ok, reason_string).

    Accepts if ANY _ACCEPT_SELECTORS element exists in the DOM OR body text
    contains any _ACCEPT_TEXT_PHRASES keyword.

    Rejects as hard-blocked only if a _BLOCK_PHRASES phrase is present.
    Otherwise rejects with 'no content found' so the caller can save artifacts.
    """
    if not body_lower:
        try:
            body_lower = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            body_lower = ""

    # Hard block check first — specific bot-wall phrases only
    for phrase in _BLOCK_PHRASES:
        if phrase in body_lower:
            return False, f"hard block phrase: {phrase!r}"

    # Liberal selector check — existence only, no waiting
    for sel in _ACCEPT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                return True, f"selector: {sel!r}"
        except Exception:
            continue

    # Text fallback — catches paywall pages that still show the abstract
    for phrase in _ACCEPT_TEXT_PHRASES:
        if phrase in body_lower:
            return True, f"text: {phrase!r}"

    return False, "no accept-selector matched and no key text found"

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


# ── Single-attempt render (fresh browser/context/page every call) ─────────────

def _one_attempt(url: str, attempt: int, timeout: int) -> bytes:
    """
    One full render attempt with a brand-new browser context.
    Raises on any failure so the caller can retry with a fresh session.
    """
    print(f"  [pw] attempt {attempt} — launching chromium", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        print(f"  [pw] attempt {attempt} — browser {browser.version}", flush=True)

        context = browser.new_context(**_CONTEXT_OPTS)
        context.add_init_script(_INIT_SCRIPT)
        page = context.new_page()

        try:
            # ── Randomized pre-navigation wait (human pacing) ──────────────
            pre_nav_ms = random.randint(1_500, 4_500)
            print(f"  [pw] attempt {attempt} — pre-nav wait {pre_nav_ms} ms",
                  flush=True)
            page.wait_for_timeout(pre_nav_ms)

            # ── Full-height viewport for long articles ─────────────────────
            page.set_viewport_size({"width": 1400, "height": 2200})

            # ── Navigate ───────────────────────────────────────────────────
            print(f"  [pw] attempt {attempt} — goto {url!r}", flush=True)
            try:
                nav = page.goto(url, wait_until="networkidle", timeout=timeout)
                print(f"  [pw] attempt {attempt} — networkidle "
                      f"http={nav.status if nav else '?'}", flush=True)
            except Exception as e:
                print(f"  [pw] attempt {attempt} — networkidle failed "
                      f"({e.__class__.__name__}) — retrying domcontentloaded",
                      flush=True)
                nav = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                print(f"  [pw] attempt {attempt} — domcontentloaded "
                      f"http={nav.status if nav else '?'}", flush=True)

            # ── Interstitial detection + guard ─────────────────────────────
            try:
                body_lower = page.inner_text("body", timeout=5_000).lower()
                interstitial_hit = any(p in body_lower for p in _INTERSTITIAL_PHRASES)
            except Exception:
                interstitial_hit = False

            print(f"  [pw] attempt {attempt} — interstitial="
                  f"{'YES — waiting' if interstitial_hit else 'no'}  "
                  f"url={page.url!r}", flush=True)

            _wait_past_interstitial(page)

            if interstitial_hit:
                print(f"  [pw] attempt {attempt} — post-interstitial "
                      f"url={page.url!r}", flush=True)

            # ── Gather debug info before validation ───────────────────────
            final_url = page.url
            try:
                page_title = page.title()
            except Exception:
                page_title = "(title unavailable)"
            try:
                body_full  = page.inner_text("body", timeout=5_000)
                body_lower = body_full.lower()
                body_preview = body_full[:500]
            except Exception:
                body_full    = ""
                body_lower   = ""
                body_preview = "(body unavailable)"

            # ── Liberal article validation ─────────────────────────────────
            article_ok, val_reason = _validate_article_page(page, body_lower)

            # ── Full debug report (all 10 points) ─────────────────────────
            print(f"  [pw] attempt {attempt} — DEBUG REPORT:", flush=True)
            print(f"    1. initial_url  = {url!r}", flush=True)
            print(f"    2. final_url    = {final_url!r}", flush=True)
            print(f"    3. interstitial = {'YES' if interstitial_hit else 'no'}", flush=True)
            print(f"    4. article_ok   = {article_ok}", flush=True)
            print(f"    5. validation   = {val_reason!r}", flush=True)
            print(f"    6. title        = {page_title!r}", flush=True)
            print(f"    7. body[:500]   = {body_preview!r}", flush=True)
            # 8 logged after pdf(); 9+10 logged below

            if not article_ok:
                # ── Save debug artifacts before raising ────────────────────
                m_pmid = re.search(r"/(\d+)/?$", url)
                pmid   = m_pmid.group(1) if m_pmid else "unknown"
                try:
                    dbg = Path(f"debug/{pmid}/attempt_{attempt}")
                    dbg.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(dbg / "screenshot.png"),
                                    full_page=True)
                    (dbg / "page.html").write_text(
                        page.content(), encoding="utf-8")
                    print(f"    ↳ debug artifacts → {dbg}/", flush=True)
                except Exception as dbg_exc:
                    print(f"    ↳ artifact save failed: {dbg_exc}", flush=True)

                print(f"    9. REJECTED", flush=True)
                print(f"   10. reason: {val_reason!r}", flush=True)
                raise ValueError(
                    f"attempt {attempt}: page rejected — {val_reason}  "
                    f"url={final_url!r}"
                )

            print(f"    9. ACCEPTED  ({val_reason})", flush=True)

            # ── Inject full-width print CSS ────────────────────────────────
            page.add_style_tag(content="""
html, body {
    width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow-x: hidden !important;
}
main, article, .article-page {
    max-width: 100% !important;
}
""")

            # ── Randomized pre-pdf settle ──────────────────────────────────
            pre_pdf_ms = random.randint(2_500, 6_000)
            print(f"  [pw] attempt {attempt} — pre-pdf settle {pre_pdf_ms} ms",
                  flush=True)
            page.wait_for_timeout(pre_pdf_ms)

            # ── Print ──────────────────────────────────────────────────────
            print(f"  [pw] attempt {attempt} — calling page.pdf() …", flush=True)
            pdf = page.pdf(**_PDF_OPTS)
            valid = pdf[:4] == b"%PDF" if pdf else False
            print(f"  [pw] attempt {attempt} — pdf() → {len(pdf):,} bytes  "
                  f"valid={valid}  url={page.url!r}", flush=True)
            return pdf

        finally:
            page.close()
            context.close()
            browser.close()
            print(f"  [pw] attempt {attempt} — browser closed", flush=True)


# ── Core render function — up to 3 attempts with fresh context each time ──────

def _render_pdf(url: str, timeout: int = 60_000) -> bytes:
    """
    Try up to 3 times (attempt 1 + 2 retries).
    Each retry uses a completely fresh browser/context/page and cookies.
    Randomised backoff between retries to reduce request burst behaviour.
    """
    max_attempts = 3
    last_exc: Exception = RuntimeError("no attempts made")

    for attempt in range(1, max_attempts + 1):
        try:
            return _one_attempt(url, attempt, timeout)
        except Exception as exc:
            last_exc = exc
            print(f"  [pw] attempt {attempt} FAILED: {exc}", flush=True)
            if attempt < max_attempts:
                backoff_s = random.uniform(3.0, 7.0)
                print(f"  [pw] backing off {backoff_s:.1f} s before "
                      f"attempt {attempt + 1} …", flush=True)
                time.sleep(backoff_s)

    raise last_exc


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
