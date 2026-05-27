"""
Renderer Worker — Playwright/Chromium PDF service.

Endpoints:
  GET  /health         → {"ok": true, "chromium": "<version>"}
  POST /render         → JSON {"url": "..."} → PDF bytes (application/pdf)
  POST /render-batch   → JSON {"urls": [...]} → JSON {"results": [...]}

Thread-safety model:
  Each request (including each worker inside /render-batch) creates its OWN
  `with sync_playwright() as pw:` context and launches its own browser.
  sync_playwright() is NOT thread-safe when shared — the singleton approach
  introduced in a previous refactor caused all renders to fail silently.
  Per-request browsers are the correct model for threaded Flask.
"""

import base64
import random
import re
import sys
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

# ── Realistic desktop-Chrome fingerprint ─────────────────────────────────────
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
        "Accept": (
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

_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', {
        get: () => Object.assign([1,2,3,4,5], { item: (i) => i }),
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    const _cdcKeys = Object.keys(window).filter(k => k.startsWith('cdc_'));
    _cdcKeys.forEach(k => { try { delete window[k]; } catch(_) {} });
"""

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

# ── Content detection ──────────────────────────────────────────────────────────
_INTERSTITIAL_PHRASES = [
    "checking your browser", "automatically redirected",
    "verifying you are human", "just a moment", "please wait",
    "ddos protection", "ray id", "enable javascript", "browser verification",
]
_ARTICLE_SELECTORS = [
    "#article-details", ".abstract-content", "#abstract",
    ".article-page", ".full-view", "article", ".article",
    ".content-main", "main",
]
_ACCEPT_SELECTORS = [
    "#article-details", "main", "article",
    ".heading-title", ".abstract-content", "#abstract",
]
_ACCEPT_TEXT_PHRASES = ["abstract", "pubmed"]
_BLOCK_PHRASES       = [
    "checking your browser", "cf-browser-verification",
    "access denied", "403 forbidden",
]

_MAX_CONCURRENT = 3


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
            return False, f"hard block: {phrase!r}"
    for sel in _ACCEPT_SELECTORS:
        try:
            if page.query_selector(sel):
                return True, f"selector: {sel!r}"
        except Exception:
            continue
    for phrase in _ACCEPT_TEXT_PHRASES:
        if phrase in body_lower:
            return True, f"text: {phrase!r}"
    return False, "no accept-selector and no key text"


_INTERSTITIAL_CLEAR_JS = """
(phrases) => {
    const txt = document.body ? document.body.innerText.toLowerCase() : '';
    return !phrases.some(p => txt.includes(p));
}
"""


def _wait_for_article(page, prefix: str, timeout_ms: int = 10_000) -> bool:
    per_ms = max(1_000, timeout_ms // len(_ARTICLE_SELECTORS))
    for sel in _ARTICLE_SELECTORS:
        try:
            page.wait_for_selector(sel, state="visible", timeout=per_ms)
            print(f"{prefix} [selector-visible] {sel!r}", flush=True)
            return True
        except Exception:
            continue
    print(f"{prefix} [selector-visible] none matched", flush=True)
    return False


def _wait_past_interstitial(page, prefix: str, timeout_ms: int = 35_000) -> None:
    try:
        body = page.inner_text("body", timeout=5_000).lower()
    except Exception:
        return
    found = [p for p in _INTERSTITIAL_PHRASES if p in body]
    if not found:
        _wait_for_article(page, prefix, timeout_ms=8_000)
        return
    print(f"{prefix} [interstitial] detected: {found}", flush=True)
    try:
        page.wait_for_function(
            _INTERSTITIAL_CLEAR_JS, arg=_INTERSTITIAL_PHRASES,
            timeout=timeout_ms, polling=500,
        )
        print(f"{prefix} [interstitial] cleared", flush=True)
    except Exception as exc:
        err = str(exc).lower()
        if any(k in err for k in ("navigat", "detach", "target closed", "execution context")):
            print(f"{prefix} [interstitial] redirect fired — waiting for new page", flush=True)
        else:
            print(f"{prefix} [interstitial] timeout — proceeding", flush=True)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    _wait_for_article(page, prefix, timeout_ms=10_000)
    page.wait_for_timeout(800)


# ── Single-attempt render — owns its own playwright + browser ─────────────────

def _one_attempt(url: str, attempt: int, timeout: int) -> bytes:
    """
    One full render attempt.  Creates a fresh sync_playwright() context,
    browser, page — all owned by the calling thread.  Thread-safe.
    """
    pmc    = _is_pmc(url)
    prefix = f"  [pw a{attempt}]"
    print(f"{prefix} START  url={url!r}  pmc={pmc}", flush=True)

    with sync_playwright() as pw:
        print(f"{prefix} playwright started", flush=True)

        browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        print(f"{prefix} browser launched  ver={browser.version}", flush=True)

        context = browser.new_context(**_CONTEXT_OPTS)
        context.add_init_script(_INIT_SCRIPT)
        print(f"{prefix} context created", flush=True)

        page = context.new_page()
        page.set_viewport_size({"width": 1400, "height": 2200})
        print(f"{prefix} page created", flush=True)

        try:
            # ── Pre-nav human pacing (skip for PMC) ───────────────
            if not pmc:
                pre_ms = random.randint(300, 800)
                page.wait_for_timeout(pre_ms)

            # ── Navigate ──────────────────────────────────────────
            nav_timeout = int(timeout * 0.6)
            print(f"{prefix} goto {url!r}", flush=True)
            try:
                nav = page.goto(url, wait_until="domcontentloaded",
                                timeout=nav_timeout)
                status = nav.status if nav else "?"
                print(f"{prefix} goto complete  http={status}", flush=True)
            except Exception as e:
                print(f"{prefix} goto FAILED: {e.__class__.__name__}: {e}",
                      flush=True)
                raise

            # ── Wait for content ──────────────────────────────────
            if pmc:
                print(f"{prefix} PMC fast-path — waiting for article selector",
                      flush=True)
                _wait_for_article(page, prefix, timeout_ms=10_000)
            else:
                _wait_past_interstitial(page, prefix)

            # ── Validate page ─────────────────────────────────────
            print(f"{prefix} validating page  url={page.url!r}", flush=True)
            try:
                body_lower = page.inner_text("body", timeout=5_000).lower()
            except Exception:
                body_lower = ""

            article_ok, val_reason = _validate_article_page(page, body_lower)
            print(f"{prefix} validation  ok={article_ok}  reason={val_reason!r}",
                  flush=True)

            if not article_ok:
                m    = re.search(r"/(\d+)/?$", url)
                pmid = m.group(1) if m else "unknown"
                try:
                    dbg = Path(f"debug/{pmid}/attempt_{attempt}")
                    dbg.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(dbg / "screenshot.png"), full_page=True)
                    (dbg / "page.html").write_text(page.content(), encoding="utf-8")
                    print(f"{prefix} debug artifacts → {dbg}/", flush=True)
                except Exception as dbg_exc:
                    print(f"{prefix} artifact save failed: {dbg_exc}", flush=True)
                raise ValueError(
                    f"page rejected — {val_reason}  url={page.url!r}"
                )

            # ── Inject full-width print CSS ───────────────────────
            page.add_style_tag(content="""
html, body { width:100% !important; margin:0 !important;
             padding:0 !important; overflow-x:hidden !important; }
main, article, .article-page { max-width:100% !important; }
""")

            # ── Pre-PDF settle ────────────────────────────────────
            settle_ms = 800 if pmc else random.randint(1_000, 1_500)
            print(f"{prefix} pre-pdf settle {settle_ms} ms", flush=True)
            page.wait_for_timeout(settle_ms)

            # ── Print to PDF ──────────────────────────────────────
            print(f"{prefix} calling page.pdf()", flush=True)
            pdf = page.pdf(**_PDF_OPTS)
            valid = bool(pdf and pdf[:4] == b"%PDF")
            print(f"{prefix} pdf() → {len(pdf):,} bytes  valid={valid}",
                  flush=True)

            if not valid:
                raise ValueError(f"page.pdf() returned {len(pdf)} bytes — not a valid PDF")

            print(f"{prefix} SUCCESS", flush=True)
            return pdf

        except Exception:
            print(f"{prefix} EXCEPTION:", flush=True)
            traceback.print_exc()
            raise

        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            print(f"{prefix} browser closed", flush=True)


# ── Core render — up to 3 attempts ────────────────────────────────────────────

def _render_pdf(url: str, timeout: int = 45_000) -> bytes:
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, 4):
        try:
            return _one_attempt(url, attempt, timeout)
        except Exception as exc:
            last_exc = exc
            print(f"  [pw] attempt {attempt} FAILED: {exc}", flush=True)
            if attempt < 3:
                backoff = random.uniform(2.0, 4.0)
                print(f"  [pw] backoff {backoff:.1f}s", flush=True)
                import time; time.sleep(backoff)
    raise last_exc


# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/health")
def health():
    """Liveness probe — launches a throw-away browser to confirm Chromium works."""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            ver = browser.version
            browser.close()
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 503
    return jsonify(ok=True, service="renderer-worker", version="2.1", chromium=ver)


@app.post("/render")
def render():
    """Single-URL render."""
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="url is required"), 400

    print(f"\n[render] START  url={url!r}", flush=True)
    try:
        pdf_bytes = _render_pdf(url)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[render] FAILED  url={url!r}\n{tb}", flush=True)
        return jsonify(error=str(exc), traceback=tb), 500

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        msg = f"page.pdf() returned {len(pdf_bytes)} bytes — not a valid PDF"
        print(f"[render] INVALID PDF  url={url!r}", flush=True)
        return jsonify(error=msg), 502

    print(f"[render] SUCCESS  {len(pdf_bytes):,} bytes  url={url!r}", flush=True)
    return Response(
        pdf_bytes,
        mimetype = "application/pdf",
        headers  = {"X-PDF-Size": str(len(pdf_bytes))},
    )


@app.post("/render-batch")
def render_batch():
    """
    Render multiple URLs in parallel (≤_MAX_CONCURRENT at a time).
    Each parallel worker owns its own playwright/browser — fully thread-safe.

    Request:  {"urls": ["https://...", ...]}
    Response: {"results": [{"url":..., "ok":true,  "pdf_b64":"..."},
                            {"url":..., "ok":false, "error":"..."}]}
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    if not urls:
        return jsonify(error="urls list required"), 400

    print(f"\n[batch] START  {len(urls)} URL(s)  max_concurrent={_MAX_CONCURRENT}",
          flush=True)

    results: list[dict | None] = [None] * len(urls)

    def _render_one(idx: int, url: str) -> tuple[int, dict]:
        print(f"  [batch] [{idx+1}/{len(urls)}] START  {url!r}", flush=True)
        try:
            pdf = _render_pdf(url)
            print(f"  [batch] [{idx+1}/{len(urls)}] SUCCESS  {len(pdf):,} bytes  {url!r}",
                  flush=True)
            return idx, {"url": url, "ok": True,
                         "pdf_b64": base64.b64encode(pdf).decode()}
        except Exception as exc:
            print(f"  [batch] [{idx+1}/{len(urls)}] FAILED  {exc!r}  {url!r}",
                  flush=True)
            return idx, {"url": url, "ok": False, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT) as pool:
        futures = [pool.submit(_render_one, i, u) for i, u in enumerate(urls)]
        for f in as_completed(futures):
            idx, res = f.result()
            results[idx] = res

    ok   = sum(1 for r in results if r and r.get("ok"))
    fail = len(results) - ok
    print(f"[batch] DONE  {ok} ok, {fail} failed", flush=True)
    return jsonify(results=results)


if __name__ == "__main__":
    port = 7777
    print(f"\n  Renderer Worker v2.1 — http://localhost:{port}")
    print(f"  Thread model: per-request playwright (thread-safe)\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
