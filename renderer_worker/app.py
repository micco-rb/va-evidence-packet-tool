"""
Renderer Worker — Playwright/Chromium PDF service.

Endpoints:
  GET  /health         → {"ok": true, "chromium": "<version>"}
  POST /render         → JSON {"url": "..."} → PDF bytes (application/pdf)
  POST /render-batch   → JSON {"urls": [...]} → JSON {"results": [...]}

A SINGLE Chromium browser is launched at startup and reused for every request.
Each request gets its own context+page (thread-safe). The browser is
auto-reconnected if it crashes.
"""

import base64
import random
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    extra_http_headers = {
        "Accept":                    (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language":           "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile":          "?0",
        "sec-ch-ua-platform":        '"Windows"',
        "sec-fetch-dest":            "document",
        "sec-fetch-mode":            "navigate",
        "sec-fetch-site":            "none",
        "sec-fetch-user":            "?1",
    },
)

# ── Init script injected into every page ──────────────────────────────────────
_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => Object.assign([1,2,3,4,5], { item: (i) => i }),
    });
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });
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

# ── Interstitial phrases ───────────────────────────────────────────────────────
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

_ARTICLE_SELECTORS = [
    "#article-details",
    ".abstract-content",
    "#abstract",
    ".article-page",
    ".full-view",
    "article",
    ".article",
    ".content-main",
    "main",
]

_ACCEPT_SELECTORS = [
    "#article-details",
    "main",
    "article",
    ".heading-title",
    ".abstract-content",
    "#abstract",
]
_ACCEPT_TEXT_PHRASES = ["abstract", "pubmed"]

_BLOCK_PHRASES = [
    "checking your browser",
    "cf-browser-verification",
    "access denied",
    "403 forbidden",
]

# Concurrency cap for /render-batch
_MAX_CONCURRENT = 3


# ── Persistent browser singleton ──────────────────────────────────────────────
_pw_lock    = threading.Lock()
_pw_inst    = None
_browser    = None


def _get_browser():
    """Return the shared browser, launching / reconnecting as needed."""
    global _pw_inst, _browser
    with _pw_lock:
        if _pw_inst is None:
            _pw_inst = sync_playwright().start()
            print("[browser] playwright started", flush=True)
        if _browser is None or not _browser.is_connected():
            _browser = _pw_inst.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            print(f"[browser] launched chromium {_browser.version}", flush=True)
        return _browser


def _new_context_page():
    """Create a fresh context+page on the shared browser."""
    browser = _get_browser()
    context = browser.new_context(**_CONTEXT_OPTS)
    context.add_init_script(_INIT_SCRIPT)
    page = context.new_page()
    return context, page


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_pmc(url: str) -> bool:
    return "pmc.ncbi.nlm.nih.gov" in url or "/pmc/articles/" in url


def _validate_article_page(page, body_lower: str = "") -> tuple[bool, str]:
    if not body_lower:
        try:
            body_lower = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            body_lower = ""

    for phrase in _BLOCK_PHRASES:
        if phrase in body_lower:
            return False, f"hard block phrase: {phrase!r}"

    for sel in _ACCEPT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                return True, f"selector: {sel!r}"
        except Exception:
            continue

    for phrase in _ACCEPT_TEXT_PHRASES:
        if phrase in body_lower:
            return True, f"text: {phrase!r}"

    return False, "no accept-selector matched and no key text found"


_INTERSTITIAL_CLEAR_JS = """
(phrases) => {
    const txt = document.body ? document.body.innerText.toLowerCase() : '';
    return !phrases.some(p => txt.includes(p));
}
"""


def _wait_for_article(page, timeout_ms: int = 10_000) -> bool:
    per_sel_ms = max(1_000, timeout_ms // len(_ARTICLE_SELECTORS))
    for sel in _ARTICLE_SELECTORS:
        try:
            page.wait_for_selector(sel, state="visible", timeout=per_sel_ms)
            print(f"  [pw] article visible: {sel!r}", flush=True)
            return True
        except Exception:
            continue
    print(f"  [pw] no article selector matched  url={page.url!r}", flush=True)
    return False


def _wait_past_interstitial(page, interstitial_timeout_ms: int = 35_000) -> None:
    try:
        body_text = page.inner_text("body", timeout=5_000).lower()
    except Exception:
        return

    found = [p for p in _INTERSTITIAL_PHRASES if p in body_text]
    if not found:
        _wait_for_article(page, timeout_ms=8_000)
        return

    print(f"  [pw] interstitial detected: {found}", flush=True)

    try:
        page.wait_for_function(
            _INTERSTITIAL_CLEAR_JS,
            arg     = _INTERSTITIAL_PHRASES,
            timeout = interstitial_timeout_ms,
            polling = 500,
        )
        print(f"  [pw] interstitial cleared  url={page.url!r}", flush=True)
    except Exception as exc:
        err_str = str(exc).lower()
        if any(k in err_str for k in ("navigat", "detach", "target closed",
                                       "execution context")):
            print(f"  [pw] redirect detected during interstitial wait", flush=True)
        else:
            print(f"  [pw] interstitial wait timed out — proceeding", flush=True)

    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass

    _wait_for_article(page, timeout_ms=10_000)
    page.wait_for_timeout(800)


# ── Single-attempt render (reuses persistent browser) ─────────────────────────

def _one_attempt(url: str, attempt: int, timeout: int) -> bytes:
    """
    One render attempt. Uses the persistent browser — no relaunch.
    Creates a new context+page, renders, then closes context+page only.
    """
    pmc = _is_pmc(url)
    print(f"  [pw] attempt {attempt} — new context  pmc={pmc}  url={url!r}",
          flush=True)

    context, page = _new_context_page()

    try:
        # Full-height viewport for long articles
        page.set_viewport_size({"width": 1400, "height": 2200})

        # ── Pre-nav human pacing (minimal; skip entirely for PMC) ──────
        if not pmc:
            pre_nav_ms = random.randint(300, 800)
            page.wait_for_timeout(pre_nav_ms)

        # ── Navigate ───────────────────────────────────────────────────
        nav_timeout = int(timeout * 0.6)  # leave headroom for pdf()
        print(f"  [pw] attempt {attempt} — goto {url!r}", flush=True)
        try:
            nav = page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
            print(f"  [pw] attempt {attempt} — domcontentloaded "
                  f"http={nav.status if nav else '?'}", flush=True)
        except Exception as e:
            print(f"  [pw] attempt {attempt} — goto failed ({e.__class__.__name__})",
                  flush=True)
            raise

        # ── PMC fast-path: just wait for article selector ──────────────
        if pmc:
            _wait_for_article(page, timeout_ms=10_000)
        else:
            _wait_past_interstitial(page)

        # ── Gather page state ──────────────────────────────────────────
        final_url   = page.url
        try:
            body_lower = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            body_lower = ""

        article_ok, val_reason = _validate_article_page(page, body_lower)
        print(f"  [pw] attempt {attempt} — ok={article_ok} reason={val_reason!r} "
              f"url={final_url!r}", flush=True)

        if not article_ok:
            m_pmid = re.search(r"/(\d+)/?$", url)
            pmid   = m_pmid.group(1) if m_pmid else "unknown"
            try:
                dbg = Path(f"debug/{pmid}/attempt_{attempt}")
                dbg.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(dbg / "screenshot.png"), full_page=True)
                (dbg / "page.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            raise ValueError(
                f"attempt {attempt}: page rejected — {val_reason}  "
                f"url={final_url!r}"
            )

        # ── Full-width print CSS ───────────────────────────────────────
        page.add_style_tag(content="""
html, body { width: 100% !important; margin: 0 !important;
             padding: 0 !important; overflow-x: hidden !important; }
main, article, .article-page { max-width: 100% !important; }
""")

        # ── Pre-pdf settle (much shorter than before) ──────────────────
        pre_pdf_ms = 800 if pmc else random.randint(1_000, 1_500)
        page.wait_for_timeout(pre_pdf_ms)

        # ── Print to PDF ───────────────────────────────────────────────
        print(f"  [pw] attempt {attempt} — calling page.pdf() …", flush=True)
        pdf = page.pdf(**_PDF_OPTS)
        valid = pdf[:4] == b"%PDF" if pdf else False
        print(f"  [pw] attempt {attempt} — pdf() → {len(pdf):,} bytes  "
              f"valid={valid}", flush=True)
        return pdf

    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        print(f"  [pw] attempt {attempt} — context closed", flush=True)


# ── Core render — up to 3 attempts ────────────────────────────────────────────

def _render_pdf(url: str, timeout: int = 45_000) -> bytes:
    max_attempts = 3
    last_exc: Exception = RuntimeError("no attempts made")

    for attempt in range(1, max_attempts + 1):
        try:
            return _one_attempt(url, attempt, timeout)
        except Exception as exc:
            last_exc = exc
            print(f"  [pw] attempt {attempt} FAILED: {exc}", flush=True)
            if attempt < max_attempts:
                # Reconnect browser between retries if it died
                backoff_s = random.uniform(1.5, 3.5)
                print(f"  [pw] backoff {backoff_s:.1f}s before attempt {attempt + 1}",
                      flush=True)
                time.sleep(backoff_s)
                # Re-check browser health; _get_browser() will reconnect if needed
                try:
                    _get_browser()
                except Exception as be:
                    print(f"  [pw] browser reconnect error: {be}", flush=True)

    raise last_exc


# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/health")
def health():
    try:
        browser = _get_browser()
        ver = browser.version
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 503
    return jsonify(ok=True, service="renderer-worker", version="2.0", chromium=ver)


@app.post("/render")
def render():
    """Single-URL render — kept for backwards compatibility."""
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="url is required"), 400

    print(f"\n[render] url={url!r}", flush=True)
    try:
        pdf_bytes = _render_pdf(url)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[render] EXCEPTION:\n{tb}", flush=True)
        return jsonify(error=str(exc), traceback=tb), 500

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        msg = f"page.pdf() returned {len(pdf_bytes)} bytes — not a valid PDF"
        return jsonify(error=msg), 502

    print(f"[render] SUCCESS  {len(pdf_bytes):,} bytes", flush=True)
    return Response(
        pdf_bytes,
        mimetype = "application/pdf",
        headers  = {"X-PDF-Size": str(len(pdf_bytes))},
    )


@app.post("/render-batch")
def render_batch():
    """
    Render multiple URLs in parallel (up to _MAX_CONCURRENT at a time).
    Request:  {"urls": ["https://...", ...]}
    Response: {"results": [{"url":..., "ok":true,  "pdf_b64":"..."},
                            {"url":..., "ok":false, "error":"..."}]}
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    if not urls:
        return jsonify(error="urls list required"), 400

    print(f"\n[batch] {len(urls)} URL(s) — max_concurrent={_MAX_CONCURRENT}",
          flush=True)

    results: list[dict | None] = [None] * len(urls)

    def _render_one(idx: int, url: str) -> tuple[int, dict]:
        print(f"  [batch] start [{idx+1}/{len(urls)}] {url!r}", flush=True)
        try:
            pdf = _render_pdf(url)
            print(f"  [batch] done  [{idx+1}/{len(urls)}] "
                  f"{len(pdf):,} bytes  {url!r}", flush=True)
            return idx, {
                "url":     url,
                "ok":      True,
                "pdf_b64": base64.b64encode(pdf).decode(),
            }
        except Exception as exc:
            print(f"  [batch] fail  [{idx+1}/{len(urls)}] "
                  f"{exc}  {url!r}", flush=True)
            return idx, {"url": url, "ok": False, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT) as pool:
        futures = [pool.submit(_render_one, i, u) for i, u in enumerate(urls)]
        for f in as_completed(futures):
            idx, res = f.result()
            results[idx] = res

    ok_count   = sum(1 for r in results if r and r.get("ok"))
    fail_count = len(results) - ok_count
    print(f"[batch] complete — {ok_count} ok, {fail_count} failed", flush=True)

    return jsonify(results=results)


# ── Startup — pre-warm browser ─────────────────────────────────────────────────
if __name__ == "__main__":
    port = 7777
    print(f"\n  Renderer Worker v2 — http://localhost:{port}")
    print(f"  Pre-warming Chromium …")
    try:
        _get_browser()
        print(f"  Browser ready ✓\n")
    except Exception as exc:
        print(f"  WARNING: browser pre-warm failed: {exc}\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
