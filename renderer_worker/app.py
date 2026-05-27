"""
Renderer Worker v2.3 — Production-hardened Playwright PDF service.

Endpoints:
  GET  /health         → {"ok": true, "chromium": "<version>"}
  POST /render         → JSON {"url": "..."} → PDF bytes (application/pdf)
  POST /render-batch   → JSON {"urls": [...]} → JSON {"results": [...]}

Key design decisions (v2.3):
  • Per-article wall-clock deadline (MAX_ARTICLE_SECONDS = 45).  Deadline is
    checked between every major stage so a hung PMC page can't freeze the batch.
  • networkidle REMOVED for NCBI/PubMed/PMC.  Only domcontentloaded + selector
    wait + short stabilisation delay.  networkidle stalls on ad/tracker requests.
  • MAX_CONCURRENT = 3 (Railway paid plan; each browser is isolated per-thread).
  • 2 attempts max for NCBI (tight budget); 3 for generic sites.
  • Enhanced debug artifacts: screenshot + page.html + debug_info.json on failure.
  • No abstract/text/transcription fallback — browser-rendered PDF only.
"""

import base64
import json
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


# ── Tuning constants ───────────────────────────────────────────────────────────
MAX_ARTICLE_SECONDS = 45      # hard wall-clock cap per article (all attempts)
MAX_CONCURRENT      = 3       # concurrent browser instances (Railway paid)
_PW_TIMEOUT_MS      = 38_000  # playwright-level timeout per attempt (ms)


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
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]

# ── User agents ────────────────────────────────────────────────────────────────
_UA_GENERIC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_UA_NCBI = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ── Context options ────────────────────────────────────────────────────────────
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

# ── Stealth init scripts ────────────────────────────────────────────────────────
_INIT_SCRIPT_BASE = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
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
            p.item = (i) => p[i]; p.namedItem = (n) => p.find(x=>x.name===n)||null;
            p.refresh = ()=>{}; return p;
        }
    });
    Object.defineProperty(navigator, 'languages',           { get: () => ['en-US','en'] });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory',        { get: () => 8 });
    Object.defineProperty(navigator, 'maxTouchPoints',      { get: () => 0 });
    window.chrome = {
        app: { isInstalled: false,
               InstallState:  { DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed' },
               RunningState:  { CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running' } },
        runtime: { id: undefined }
    };
    if (navigator.permissions && navigator.permissions.query) {
        const _oq = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (p) => {
            if (p && p.name === 'notifications')
                return Promise.resolve({ state: 'default', onchange: null });
            return _oq(p);
        };
    }
    Object.keys(window).filter(k => k.startsWith('cdc_'))
          .forEach(k => { try { delete window[k]; } catch(_) {} });
"""

_INIT_SCRIPT_NCBI = _INIT_SCRIPT_BASE + """
    Object.defineProperty(screen, 'colorDepth',  { get: () => 24 });
    Object.defineProperty(screen, 'pixelDepth',  { get: () => 24 });
    Object.defineProperty(window, 'outerWidth',  { get: () => 1366 });
    Object.defineProperty(window, 'outerHeight', { get: () => 768 });
    Object.defineProperty(window, 'innerWidth',  { get: () => 1366 });
    Object.defineProperty(window, 'innerHeight', { get: () => 768 });
    delete window.__playwright; delete window.__pw_manual;
"""

# ── PDF print options ──────────────────────────────────────────────────────────
_PDF_OPTS = dict(
    format               = "Letter",
    print_background     = True,
    prefer_css_page_size = True,
    margin               = dict(top="0.9in", left="0.4in", right="0.42in", bottom="0.25in"),
)

# ── Selectors / content signals ────────────────────────────────────────────────
_NCBI_SELECTORS = [
    "#article-details", ".abstract-content", "#abstract",
    ".full-text", ".article-details", "#full-view-heading",
    ".pmc-article", "#mc-main-content", "#full-view",
]
_GENERIC_SELECTORS = [
    "article", ".article-page", ".full-view",
    ".article", ".content-main", "main",
]
_ALL_SELECTORS = _NCBI_SELECTORS + _GENERIC_SELECTORS

_ACCEPT_SELECTORS    = ["#article-details", "main", "article",
                        ".heading-title", ".abstract-content",
                        "#abstract", "#full-view-heading"]
_ACCEPT_TEXT         = ["abstract", "pubmed", "ncbi", "doi"]
_BLOCK_TEXT          = ["checking your browser", "cf-browser-verification",
                        "access denied", "403 forbidden"]
_INTERSTITIAL_TEXT   = [
    "checking your browser", "automatically redirected",
    "verifying you are human", "just a moment", "please wait",
    "ddos protection", "ray id", "enable javascript", "browser verification",
]
_INTERSTITIAL_CLEAR_JS = """
(phrases) => {
    const txt = document.body ? document.body.innerText.toLowerCase() : '';
    return !phrases.some(p => txt.includes(p));
}
"""


# ── Classifiers ────────────────────────────────────────────────────────────────
def _is_ncbi(url: str) -> bool:
    return any(h in url for h in (
        "pubmed.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "ncbi.nlm.nih.gov",
    ))

def _is_pmc(url: str) -> bool:
    return "pmc.ncbi.nlm.nih.gov" in url or "/pmc/articles/" in url


# ── Deadline helper ────────────────────────────────────────────────────────────
class _DeadlineExceeded(RuntimeError):
    pass

def _check(deadline: float, stage: str) -> None:
    remaining = deadline - time.time()
    if remaining <= 0:
        raise _DeadlineExceeded(f"wall-clock deadline exceeded at {stage!r}")
    return remaining


# ── Page helpers ───────────────────────────────────────────────────────────────
def _validate_page(page, body_lower: str = "") -> tuple[bool, str]:
    if not body_lower:
        try:
            body_lower = page.inner_text("body", timeout=5_000).lower()
        except Exception:
            body_lower = ""
    for phrase in _BLOCK_TEXT:
        if phrase in body_lower:
            return False, f"hard block: {phrase!r}"
    for sel in _ACCEPT_SELECTORS:
        try:
            if page.query_selector(sel):
                return True, f"selector: {sel!r}"
        except Exception:
            continue
    for phrase in _ACCEPT_TEXT:
        if phrase in body_lower:
            return True, f"text: {phrase!r}"
    return False, "no accept-selector and no key text"


def _wait_for_article(page, prefix: str, selectors: list,
                       timeout_ms: int = 10_000) -> bool:
    per_ms = max(1_000, timeout_ms // max(len(selectors), 1))
    for sel in selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=per_ms)
            print(f"{prefix} [article-visible] {sel!r}", flush=True)
            return True
        except Exception:
            continue
    print(f"{prefix} [article-visible] none matched", flush=True)
    return False


def _wait_past_interstitial(page, prefix: str,
                             timeout_ms: int = 25_000,
                             skip_networkidle: bool = False) -> None:
    """
    Wait for Cloudflare / bot-check interstitials to clear.
    skip_networkidle=True for NCBI — networkidle stalls on tracker requests.
    """
    try:
        body = page.inner_text("body", timeout=4_000).lower()
    except Exception:
        return
    found = [p for p in _INTERSTITIAL_TEXT if p in body]
    if not found:
        return
    print(f"{prefix} [interstitial] detected: {found}", flush=True)
    try:
        page.wait_for_function(
            _INTERSTITIAL_CLEAR_JS, arg=_INTERSTITIAL_TEXT,
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

    # networkidle deliberately skipped for NCBI — tracker/ad requests never settle
    if not skip_networkidle:
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass


def _save_debug_artifacts(page, url: str, label: str,
                           elapsed_s: float | None = None,
                           http_status: int | str | None = None,
                           error: str | None = None) -> None:
    """Save screenshot + page.html + debug_info.json for post-mortem inspection."""
    try:
        m    = re.search(r"/(\d+)/?(?:\?.*)?$", url)
        slug = m.group(1) if m else re.sub(r"[^a-z0-9]", "_", url.lower())[:40]
        dbg  = Path(f"debug/{slug}/{label}")
        dbg.mkdir(parents=True, exist_ok=True)

        page.screenshot(path=str(dbg / "screenshot.png"), full_page=True)
        (dbg / "page.html").write_text(page.content(), encoding="utf-8")

        info = {
            "url":         url,
            "label":       label,
            "timestamp":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_s":   round(elapsed_s, 2) if elapsed_s is not None else None,
            "http_status": http_status,
            "final_url":   page.url,
            "error":       error,
        }
        (dbg / "debug_info.json").write_text(
            json.dumps(info, indent=2), encoding="utf-8"
        )
        print(f"  [debug] artifacts → {dbg}/", flush=True)
    except Exception as e:
        print(f"  [debug] artifact save failed: {e}", flush=True)


# ── Single attempt ─────────────────────────────────────────────────────────────
def _one_attempt(url: str, attempt: int, deadline: float) -> bytes:
    """
    Full render attempt in an isolated playwright/browser/context/page.
    All major stages check the wall-clock deadline; stalls raise _DeadlineExceeded.
    networkidle is skipped for NCBI — use selector + short settle instead.
    """
    t0   = time.time()
    ncbi = _is_ncbi(url)
    pmc  = _is_pmc(url)
    pfx  = f"  [pw a{attempt}{'N' if ncbi else ''}]"

    ctx_opts    = _CONTEXT_OPTS_NCBI    if ncbi else _CONTEXT_OPTS_GENERIC
    init_script = _INIT_SCRIPT_NCBI     if ncbi else _INIT_SCRIPT_BASE
    selectors   = _NCBI_SELECTORS       if ncbi else _ALL_SELECTORS

    remaining_s = _check(deadline, "start")
    # Nav timeout = min(20s, 55% of remaining wall-clock) in ms
    nav_ms = min(20_000, int(remaining_s * 0.55 * 1_000))

    print(f"{pfx} START  ncbi={ncbi} pmc={pmc}  remaining={remaining_s:.1f}s  "
          f"nav_ms={nav_ms}  url={url!r}", flush=True)

    with sync_playwright() as pw:
        print(f"{pfx} playwright started", flush=True)
        browser = pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        print(f"{pfx} browser launched  ver={browser.version}", flush=True)

        context = browser.new_context(**ctx_opts)
        context.add_init_script(init_script)
        page    = context.new_page()
        page.set_viewport_size({"width": 1366, "height": 768})

        http_status: int | str = "?"

        try:
            # ── Pre-nav pacing ─────────────────────────────────────
            _check(deadline, "pre-nav")
            pre_ms = random.randint(500, 900) if ncbi else random.randint(100, 300)
            page.wait_for_timeout(pre_ms)

            # ── Navigate ───────────────────────────────────────────
            _check(deadline, "navigate")
            print(f"{pfx} goto {url!r}  nav_ms={nav_ms}", flush=True)
            try:
                nav = page.goto(url, wait_until="domcontentloaded",
                                timeout=nav_ms)
                http_status = nav.status if nav else "?"
                print(f"{pfx} goto ok  http={http_status}  url={page.url!r}",
                      flush=True)
            except Exception as e:
                print(f"{pfx} goto FAILED: {e.__class__.__name__}: {e}", flush=True)
                raise

            # ── Wait for content ───────────────────────────────────
            _check(deadline, "wait-content")

            if ncbi:
                # Interstitial wait — no networkidle for NCBI
                _wait_past_interstitial(page, pfx, timeout_ms=20_000,
                                        skip_networkidle=True)
                _check(deadline, "post-interstitial")

                # Article selector wait
                art_budget_ms = max(4_000, int((_check(deadline, "article-wait") - 2) * 1_000))
                found = _wait_for_article(page, pfx, selectors,
                                          timeout_ms=min(art_budget_ms, 12_000))
                print(f"{pfx} article selector found={found}", flush=True)

                # Reload-retry only if time allows (need ≥ 12s)
                if not found and _check(deadline, "reload-check") >= 12:
                    print(f"{pfx} [ncbi-reload] no selector — reloading", flush=True)
                    _save_debug_artifacts(
                        page, url, f"a{attempt}_before_reload",
                        elapsed_s=time.time() - t0,
                        http_status=http_status,
                        error="no article selector before reload",
                    )
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=nav_ms)
                        print(f"{pfx} [ncbi-reload] reloaded  url={page.url!r}",
                              flush=True)
                        _check(deadline, "post-reload")
                        _wait_for_article(page, pfx, selectors, timeout_ms=8_000)
                    except _DeadlineExceeded:
                        raise
                    except Exception as re_exc:
                        print(f"{pfx} [ncbi-reload] failed: {re_exc}", flush=True)
            else:
                _wait_past_interstitial(page, pfx, timeout_ms=25_000,
                                        skip_networkidle=False)
                _check(deadline, "post-interstitial-generic")
                _wait_for_article(page, pfx, selectors, timeout_ms=8_000)

            # ── Validate ───────────────────────────────────────────
            _check(deadline, "validate")
            print(f"{pfx} validating page  url={page.url!r}", flush=True)
            try:
                body_lower = page.inner_text("body", timeout=4_000).lower()
            except Exception:
                body_lower = ""

            ok, reason = _validate_page(page, body_lower)
            print(f"{pfx} validation  ok={ok}  reason={reason!r}", flush=True)

            if not ok:
                _save_debug_artifacts(
                    page, url, f"a{attempt}_rejected",
                    elapsed_s=time.time() - t0,
                    http_status=http_status,
                    error=f"validation failed: {reason}",
                )
                raise ValueError(f"page rejected — {reason}  url={page.url!r}")

            # ── Print CSS ──────────────────────────────────────────
            page.add_style_tag(content="""
html, body { width:100% !important; margin:0 !important;
             padding:0 !important; overflow-x:hidden !important; }
main, article, .article-page, #article-details,
.abstract-content { max-width:100% !important; }
""")

            # ── Pre-PDF settle — selector wait already cleared content ─
            _check(deadline, "pre-pdf-settle")
            settle_ms = random.randint(1_200, 1_800) if ncbi else random.randint(500, 900)
            print(f"{pfx} pre-pdf settle {settle_ms} ms", flush=True)
            page.wait_for_timeout(settle_ms)

            # ── Print to PDF ───────────────────────────────────────
            _check(deadline, "print-pdf")
            print(f"{pfx} calling page.pdf()", flush=True)
            pdf = page.pdf(**_PDF_OPTS)
            valid = bool(pdf and pdf[:4] == b"%PDF")
            elapsed = time.time() - t0
            print(f"{pfx} pdf() → {len(pdf):,} bytes  valid={valid}  "
                  f"elapsed={elapsed:.1f}s", flush=True)

            if not valid:
                raise ValueError(
                    f"page.pdf() returned {len(pdf)} bytes — not a valid PDF")

            print(f"{pfx} SUCCESS  elapsed={elapsed:.1f}s", flush=True)
            return pdf

        except Exception:
            print(f"{pfx} EXCEPTION (elapsed={time.time()-t0:.1f}s):", flush=True)
            traceback.print_exc()
            raise

        finally:
            for obj in (page, context, browser):
                try:
                    obj.close()
                except Exception:
                    pass
            print(f"{pfx} closed all  elapsed={time.time()-t0:.1f}s", flush=True)


# ── Core render — retries with deadline ───────────────────────────────────────
def _render_pdf(url: str) -> bytes:
    """
    Render URL to PDF.  Honours a per-article wall-clock deadline
    (MAX_ARTICLE_SECONDS).  NCBI gets 2 attempts; generic sites get 3.
    """
    deadline   = time.time() + MAX_ARTICLE_SECONDS
    max_tries  = 2 if _is_ncbi(url) else 3
    last_exc: Exception = RuntimeError("no attempts made")

    for attempt in range(1, max_tries + 1):
        remaining = deadline - time.time()
        if remaining <= 3:
            print(f"  [pw] skipping attempt {attempt} — only {remaining:.1f}s left",
                  flush=True)
            break
        try:
            return _one_attempt(url, attempt, deadline)
        except _DeadlineExceeded as exc:
            last_exc = exc
            print(f"  [pw] attempt {attempt} deadline exceeded — stopping", flush=True)
            break
        except Exception as exc:
            last_exc = exc
            print(f"  [pw] attempt {attempt} FAILED: {exc}", flush=True)
            if attempt < max_tries:
                remaining = deadline - time.time()
                backoff   = min(random.uniform(2.0, 4.0), remaining - 3)
                if backoff < 1:
                    print(f"  [pw] no time for backoff — aborting", flush=True)
                    break
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
    return jsonify(ok=True, service="renderer-worker", version="2.3",
                   chromium=ver,
                   max_concurrent=MAX_CONCURRENT,
                   max_article_seconds=MAX_ARTICLE_SECONDS)


@app.post("/render")
def render():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="url is required"), 400

    print(f"\n[render] START  url={url!r}", flush=True)
    t0 = time.time()
    try:
        pdf_bytes = _render_pdf(url)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[render] FAILED  elapsed={time.time()-t0:.1f}s  url={url!r}\n{tb}",
              flush=True)
        return jsonify(error=str(exc), traceback=tb), 500

    if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
        return jsonify(error=f"pdf() returned {len(pdf_bytes)} bytes — not valid"), 502

    print(f"[render] SUCCESS  {len(pdf_bytes):,} bytes  "
          f"elapsed={time.time()-t0:.1f}s  url={url!r}", flush=True)
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"X-PDF-Size": str(len(pdf_bytes)),
                             "X-Elapsed-S":  f"{time.time()-t0:.1f}"})


@app.post("/render-batch")
def render_batch():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    if not urls:
        return jsonify(error="urls list required"), 400

    print(f"\n[batch] START  {len(urls)} URL(s)  "
          f"max_concurrent={MAX_CONCURRENT}  deadline_per={MAX_ARTICLE_SECONDS}s",
          flush=True)

    results: list[dict | None] = [None] * len(urls)

    def _render_one(idx: int, url: str) -> tuple[int, dict]:
        t0 = time.time()
        print(f"  [batch] [{idx+1}/{len(urls)}] START  {url!r}", flush=True)
        try:
            pdf = _render_pdf(url)
            elapsed = time.time() - t0
            print(f"  [batch] [{idx+1}/{len(urls)}] SUCCESS  "
                  f"{len(pdf):,} bytes  {elapsed:.1f}s  {url!r}", flush=True)
            return idx, {"url": url, "ok": True,
                         "elapsed_s": round(elapsed, 1),
                         "pdf_b64": base64.b64encode(pdf).decode()}
        except Exception as exc:
            elapsed = time.time() - t0
            print(f"  [batch] [{idx+1}/{len(urls)}] FAILED  "
                  f"{elapsed:.1f}s  {exc!r}  {url!r}", flush=True)
            return idx, {"url": url, "ok": False,
                         "elapsed_s": round(elapsed, 1),
                         "error": str(exc)}

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
        futures = [pool.submit(_render_one, i, u) for i, u in enumerate(urls)]
        for f in as_completed(futures):
            idx, res = f.result()
            results[idx] = res

    ok   = sum(1 for r in results if r and r.get("ok"))
    fail = len(results) - ok
    print(f"[batch] DONE  {ok} ok  {fail} failed", flush=True)
    return jsonify(results=results)


if __name__ == "__main__":
    port = 7777
    print(f"\n  Renderer Worker v2.3 — http://localhost:{port}")
    print(f"  max_concurrent={MAX_CONCURRENT}  "
          f"deadline_per_article={MAX_ARTICLE_SECONDS}s  "
          f"networkidle_for_ncbi=NO\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
