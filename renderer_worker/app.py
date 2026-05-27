"""
Renderer Worker — Playwright/Chromium PDF service.

Endpoints:
  GET  /health         → {"ok": true, "chromium": "<version>"}
  POST /render         → JSON {"url": "..."} → PDF bytes (application/pdf)
  POST /render-batch   → JSON {"urls": [...]} → JSON {"results": [...]}

Thread-safety: each request owns its own sync_playwright() + browser.
NCBI stealth: richer navigator patches, google referer, reload-retry.
"""

import base64
import random
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import Flask, request, Response, jsonify

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed.")
    sys.exit(1)


# ── Launch args ────────────────────────────────────────────────────────────────
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
    # Extra flags that reduce headless fingerprinting
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

# ── User agents ────────────────────────────────────────────────────────────────
_UA_GENERIC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# NCBI checks UA carefully — use a slightly newer version
_UA_NCBI = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ── Generic context options ────────────────────────────────────────────────────
_CONTEXT_OPTS_GENERIC = dict(
    user_agent  = _UA_GENERIC,
    viewport    = {"width": 1366, "height": 768},
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
        "sec-ch-ua":      '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":  "document",
        "sec-fetch-mode":  "navigate",
        "sec-fetch-site":  "none",
        "sec-fetch-user":  "?1",
    },
)

# ── NCBI-specific context options ──────────────────────────────────────────────
# Adds a Google referer (simulates arriving from a search) and tighter headers.
_CONTEXT_OPTS_NCBI = dict(
    user_agent  = _UA_NCBI,
    viewport    = {"width": 1366, "height": 768},
    locale      = "en-US",
    timezone_id = "America/New_York",
    extra_http_headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language":           "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "DNT":                       "1",
        "Referer":                   "https://www.google.com/",
        "sec-ch-ua":      '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":  "document",
        "sec-fetch-mode":  "navigate",
        "sec-fetch-site":  "cross-site",
        "sec-fetch-user":  "?1",
    },
)

# ── Standard stealth init script ───────────────────────────────────────────────
_INIT_SCRIPT_BASE = """
    // 1. Primary bot signal
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. Realistic plugins array
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                { name:'Chrome PDF Plugin',  filename:'internal-pdf-viewer',
                  description:'Portable Document Format' },
                { name:'Chrome PDF Viewer',  filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                  description:'' },
                { name:'Native Client',      filename:'internal-nacl-plugin',
                  description:'' },
            ];
            p.item       = (i) => p[i];
            p.namedItem  = (n) => p.find(x => x.name === n) || null;
            p.refresh    = () => {};
            return p;
        }
    });

    // 3. Languages
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

    // 4. Hardware signals (realistic desktop values)
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });
    Object.defineProperty(navigator, 'maxTouchPoints',      { get: () => 0 });

    // 5. chrome object that real Chrome exposes
    window.chrome = {
        app: {
            isInstalled: false,
            InstallState: { DISABLED:'disabled', INSTALLED:'installed',
                            NOT_INSTALLED:'not_installed' },
            RunningState: { CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run',
                            RUNNING:'running' }
        },
        runtime: { id: undefined }
    };

    // 6. Permissions API — prevent notifications check from leaking headless
    if (navigator.permissions && navigator.permissions.query) {
        const _origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (p) => {
            if (p && p.name === 'notifications') {
                return Promise.resolve({ state: 'default', onchange: null });
            }
            return _origQuery(p);
        };
    }

    // 7. Remove CDP / DevTools automation globals
    Object.keys(window).filter(k => k.startsWith('cdc_'))
          .forEach(k => { try { delete window[k]; } catch(_) {} });
"""

# ── NCBI-specific additional patches ──────────────────────────────────────────
_INIT_SCRIPT_NCBI_EXTRA = """
    // NCBI checks screen colour depth and window.outerWidth/Height
    Object.defineProperty(screen, 'colorDepth',   { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth',   { get: () => 24 });
    Object.defineProperty(window, 'outerWidth',   { get: () => 1366 });
    Object.defineProperty(window, 'outerHeight',  { get: () => 768 });
    Object.defineProperty(window, 'innerWidth',   { get: () => 1366 });
    Object.defineProperty(window, 'innerHeight',  { get: () => 768 });

    // Ensure no automation-related globals leak
    delete window.__playwright;
    delete window.__pw_manual;
"""

_INIT_SCRIPT_GENERIC = _INIT_SCRIPT_BASE
_INIT_SCRIPT_NCBI    = _INIT_SCRIPT_BASE + _INIT_SCRIPT_NCBI_EXTRA

# ── PDF options ────────────────────────────────────────────────────────────────
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

# NCBI-specific article selectors (checked first, then generic)
_NCBI_ARTICLE_SELECTORS = [
    "#article-details",
    ".abstract-content",
    "#abstract",
    ".full-text",
    ".article-details",
    "#full-view-heading",
    ".pmc-article",
    "#mc-main-content",
]
_GENERIC_ARTICLE_SELECTORS = [
    "article", ".article-page", ".full-view",
    ".article", ".content-main", "main",
]
_ALL_ARTICLE_SELECTORS = _NCBI_ARTICLE_SELECTORS + _GENERIC_ARTICLE_SELECTORS

_ACCEPT_SELECTORS    = ["#article-details", "main", "article",
                        ".heading-title", ".abstract-content", "#abstract",
                        "#full-view-heading"]
_ACCEPT_TEXT_PHRASES = ["abstract", "pubmed", "ncbi", "doi"]
_BLOCK_PHRASES       = ["checking your browser", "cf-browser-verification",
                        "access denied", "403 forbidden"]

_MAX_CONCURRENT = 3


# ── URL classifiers ────────────────────────────────────────────────────────────

def _is_ncbi(url: str) -> bool:
    return any(h in url for h in (
        "pubmed.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
    ))

def _is_pmc(url: str) -> bool:
    return "pmc.ncbi.nlm.nih.gov" in url or "/pmc/articles/" in url


# ── Page helpers ───────────────────────────────────────────────────────────────

def _validate_page(page, body_lower: str = "") -> tuple[bool, str]:
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


def _wait_for_article(page, prefix: str, selectors: list,
                       timeout_ms: int = 12_000) -> bool:
    per_ms = max(1_200, timeout_ms // len(selectors))
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=per_ms)
            print(f"{prefix} [article-visible] {sel!r}", flush=True)
            return True
        except Exception:
            continue
    print(f"{prefix} [article-visible] none matched", flush=True)
    return False


def _wait_past_interstitial(page, prefix: str, timeout_ms: int = 35_000) -> None:
    try:
        body = page.inner_text("body", timeout=5_000).lower()
    except Exception:
        return
    found = [p for p in _INTERSTITIAL_PHRASES if p in body]
    if not found:
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
        if any(k in err for k in ("navigat", "detach", "target closed",
                                   "execution context")):
            print(f"{prefix} [interstitial] redirect fired", flush=True)
        else:
            print(f"{prefix} [interstitial] timeout — proceeding", flush=True)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass


def _save_debug_screenshot(page, url: str, label: str) -> None:
    """Save a screenshot + HTML to debug/ for post-mortem inspection."""
    try:
        m    = re.search(r"/(\d+)/?$", url)
        slug = m.group(1) if m else re.sub(r"[^a-z0-9]", "_", url.lower())[:40]
        dbg  = Path(f"debug/{slug}/{label}")
        dbg.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(dbg / "screenshot.png"), full_page=True)
        (dbg / "page.html").write_text(page.content(), encoding="utf-8")
        print(f"  [debug] artifacts → {dbg}/", flush=True)
    except Exception as e:
        print(f"  [debug] screenshot failed: {e}", flush=True)


# ── Single-attempt render ──────────────────────────────────────────────────────

def _one_attempt(url: str, attempt: int, timeout: int) -> bytes:
    """
    Full render attempt in an isolated playwright/browser/context/page.
    NCBI URLs get stronger stealth context and a reload-retry pass.
    """
    ncbi   = _is_ncbi(url)
    pmc    = _is_pmc(url)
    prefix = f"  [pw a{attempt}{'N' if ncbi else ''}]"

    ctx_opts    = _CONTEXT_OPTS_NCBI    if ncbi else _CONTEXT_OPTS_GENERIC
    init_script = _INIT_SCRIPT_NCBI     if ncbi else _INIT_SCRIPT_GENERIC
    selectors   = (_NCBI_ARTICLE_SELECTORS if ncbi
                   else _ALL_ARTICLE_SELECTORS)

    print(f"{prefix} START  ncbi={ncbi} pmc={pmc}  url={url!r}", flush=True)

    with sync_playwright() as pw:
        print(f"{prefix} playwright started", flush=True)

        browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        print(f"{prefix} browser launched  ver={browser.version}", flush=True)

        context = browser.new_context(**ctx_opts)
        context.add_init_script(init_script)
        print(f"{prefix} context created (ncbi={ncbi})", flush=True)

        page = context.new_page()
        page.set_viewport_size({"width": 1366, "height": 768})
        print(f"{prefix} page created", flush=True)

        try:
            # ── Pre-nav pacing ────────────────────────────────────
            if ncbi:
                # Brief realistic pause before hitting NCBI
                pre_ms = random.randint(800, 1_500)
            else:
                pre_ms = random.randint(200, 600)
            page.wait_for_timeout(pre_ms)

            # ── Navigate ──────────────────────────────────────────
            nav_timeout = int(timeout * 0.55)
            print(f"{prefix} goto {url!r}", flush=True)
            try:
                nav = page.goto(url, wait_until="domcontentloaded",
                                timeout=nav_timeout)
                status = nav.status if nav else "?"
                print(f"{prefix} goto complete  http={status}  "
                      f"url={page.url!r}", flush=True)
            except Exception as e:
                print(f"{prefix} goto FAILED: {e.__class__.__name__}: {e}",
                      flush=True)
                raise

            # ── Wait for content ──────────────────────────────────
            if ncbi:
                # Wait for interstitial to clear, then article selectors
                _wait_past_interstitial(page, prefix, timeout_ms=30_000)
                found = _wait_for_article(page, prefix, selectors,
                                          timeout_ms=15_000)
                print(f"{prefix} article selector found={found}", flush=True)

                # ── NCBI reload-retry if first content check failed ──
                if not found:
                    print(f"{prefix} [ncbi-reload] no selector — "
                          f"saving screenshot and reloading", flush=True)
                    _save_debug_screenshot(page, url, f"a{attempt}_before_reload")
                    try:
                        page.reload(wait_until="domcontentloaded",
                                    timeout=nav_timeout)
                        print(f"{prefix} [ncbi-reload] reloaded  "
                              f"url={page.url!r}", flush=True)
                        _wait_for_article(page, prefix, selectors,
                                          timeout_ms=15_000)
                    except Exception as re_exc:
                        print(f"{prefix} [ncbi-reload] reload failed: {re_exc}",
                              flush=True)
            else:
                # Generic: handle interstitial then wait for article
                _wait_past_interstitial(page, prefix)
                _wait_for_article(page, prefix, selectors, timeout_ms=10_000)

            # ── Validate ──────────────────────────────────────────
            print(f"{prefix} validating page  url={page.url!r}", flush=True)
            try:
                body_lower = page.inner_text("body", timeout=5_000).lower()
            except Exception:
                body_lower = ""

            ok, reason = _validate_page(page, body_lower)
            print(f"{prefix} validation  ok={ok}  reason={reason!r}", flush=True)

            if not ok:
                _save_debug_screenshot(page, url, f"a{attempt}_rejected")
                raise ValueError(f"page rejected — {reason}  url={page.url!r}")

            # ── Print CSS ─────────────────────────────────────────
            page.add_style_tag(content="""
html, body { width:100% !important; margin:0 !important;
             padding:0 !important; overflow-x:hidden !important; }
main, article, .article-page, #article-details,
.abstract-content { max-width:100% !important; }
""")

            # ── Pre-PDF settle ────────────────────────────────────
            settle_ms = random.randint(1_000, 1_800) if ncbi else random.randint(800, 1_300)
            print(f"{prefix} pre-pdf settle {settle_ms} ms", flush=True)
            page.wait_for_timeout(settle_ms)

            # ── Print to PDF ──────────────────────────────────────
            print(f"{prefix} calling page.pdf()", flush=True)
            pdf = page.pdf(**_PDF_OPTS)
            valid = bool(pdf and pdf[:4] == b"%PDF")
            print(f"{prefix} pdf() → {len(pdf):,} bytes  valid={valid}",
                  flush=True)

            if not valid:
                raise ValueError(
                    f"page.pdf() returned {len(pdf)} bytes — not a valid PDF")

            print(f"{prefix} SUCCESS", flush=True)
            return pdf

        except Exception:
            print(f"{prefix} EXCEPTION:", flush=True)
            traceback.print_exc()
            raise

        finally:
            for obj, name in ((page, "page"), (context, "context"),
                               (browser, "browser")):
                try:
                    obj.close()
                except Exception:
                    pass
            print(f"{prefix} closed all", flush=True)


# ── Core render — up to 3 attempts ────────────────────────────────────────────

def _render_pdf(url: str, timeout: int = 50_000) -> bytes:
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, 4):
        try:
            return _one_attempt(url, attempt, timeout)
        except Exception as exc:
            last_exc = exc
            print(f"  [pw] attempt {attempt} FAILED: {exc}", flush=True)
            if attempt < 3:
                backoff = random.uniform(2.5, 5.0)
                print(f"  [pw] backoff {backoff:.1f}s before attempt {attempt+1}",
                      flush=True)
                time.sleep(backoff)
    raise last_exc


# ── Flask ──────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.get("/health")
def health():
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            ver = browser.version
            browser.close()
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 503
    return jsonify(ok=True, service="renderer-worker", version="2.2", chromium=ver)


@app.post("/render")
def render():
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
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"X-PDF-Size": str(len(pdf_bytes))})


@app.post("/render-batch")
def render_batch():
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
            print(f"  [batch] [{idx+1}/{len(urls)}] SUCCESS  "
                  f"{len(pdf):,} bytes  {url!r}", flush=True)
            return idx, {"url": url, "ok": True,
                         "pdf_b64": base64.b64encode(pdf).decode()}
        except Exception as exc:
            print(f"  [batch] [{idx+1}/{len(urls)}] FAILED  "
                  f"{exc!r}  {url!r}", flush=True)
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
    print(f"\n  Renderer Worker v2.2 — http://localhost:{port}")
    print(f"  NCBI stealth: enhanced  Thread model: per-request\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
