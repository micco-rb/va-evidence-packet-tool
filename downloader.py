"""
Medical research PDF downloader.

Strategy per source type:
  PMC  (pmc.ncbi.nlm.nih.gov)  → direct PDF download FIRST, Playwright fallback
  PubMed / all other URLs       → Playwright browser render (renderer worker)

No abstract extraction, no text fallback, no transcription PDFs — ever.

Download flow:
  1. PMC URLs: attempt direct PDF download via NCBI's /pdf/ endpoint +
     HTML scraping for PDF link.  Fast, no browser, no anti-bot risk.
  2. PMC failures + all non-PMC URLs → /render-batch (Playwright, ≤3 concurrent).
  3. Any remaining failures → serial /render retry + alternate URL forms.
  4. Still failed → recorded as failed.  No substitution.
"""

import base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests as _http


# ── Classifiers ───────────────────────────────────────────────────────────────

def _is_pmc(url: str) -> bool:
    return ("pmc.ncbi.nlm.nih.gov" in url or "/pmc/articles/" in url)


def _pmc_id(url: str) -> str | None:
    """Extract PMC numeric ID from a PMC URL.  Returns e.g. 'PMC10900921'."""
    m = re.search(r"PMC(\d+)", url, re.IGNORECASE)
    return f"PMC{m.group(1)}" if m else None


# ── PMC direct-download headers ───────────────────────────────────────────────

_PMC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_PMC_HEADERS = {
    "User-Agent":      _PMC_UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.google.com/",
    "DNT":             "1",
    "sec-fetch-dest":  "document",
    "sec-fetch-mode":  "navigate",
    "sec-fetch-site":  "cross-site",
}
_PDF_HEADERS = {
    **_PMC_HEADERS,
    "Accept": "application/pdf,*/*;q=0.9",
}


# ── PMC direct PDF download ───────────────────────────────────────────────────

def _pmc_direct_pdf(url: str) -> bytes | None:
    """
    Try to download a PMC article PDF directly, without browser rendering.

    Strategy:
      1. Construct canonical PDF URL from PMC ID:
         https://pmc.ncbi.nlm.nih.gov/articles/PMCxxxxxxx/pdf/
      2. If that fails, GET the article HTML page and scrape for a PDF link.
      3. Validate response is a real PDF (%PDF magic bytes).

    Returns PDF bytes on success, None on any failure.
    """
    pmc_id = _pmc_id(url)
    if not pmc_id:
        print(f"  [pmc-direct] could not extract PMC ID from {url!r}", flush=True)
        return None

    canonical_page = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
    direct_pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/pdf/"

    print(f"  [pmc-direct] {pmc_id}  trying {direct_pdf_url!r}", flush=True)

    # ── Attempt 1: canonical /pdf/ endpoint ───────────────────────────────
    pdf = _fetch_pdf(direct_pdf_url, label="pmc-pdf-endpoint")
    if pdf:
        return pdf

    # ── Attempt 2: scrape article page for PDF link ───────────────────────
    print(f"  [pmc-direct] scraping {canonical_page!r} for PDF link", flush=True)
    try:
        r = _http.get(canonical_page, headers=_PMC_HEADERS,
                      timeout=20, allow_redirects=True)
        if r.status_code == 200:
            pdf_links = _extract_pdf_links(r.text, base="https://pmc.ncbi.nlm.nih.gov")
            print(f"  [pmc-direct] found {len(pdf_links)} PDF link(s) in page", flush=True)
            for link in pdf_links:
                pdf = _fetch_pdf(link, label="pmc-scraped-link")
                if pdf:
                    return pdf
        else:
            print(f"  [pmc-direct] page HTTP {r.status_code}  {canonical_page!r}",
                  flush=True)
    except Exception as exc:
        print(f"  [pmc-direct] scrape error: {exc}", flush=True)

    print(f"  [pmc-direct] direct download failed — will fall back to renderer  "
          f"{pmc_id}", flush=True)
    return None


def _fetch_pdf(url: str, label: str = "") -> bytes | None:
    """GET a URL and return bytes only if the response is a valid PDF."""
    tag = f"[{label}] " if label else ""
    try:
        r = _http.get(url, headers=_PDF_HEADERS, timeout=25,
                      allow_redirects=True, stream=False)
        if r.status_code != 200:
            print(f"  {tag}HTTP {r.status_code}  {url!r}", flush=True)
            return None
        data = r.content
        if data[:4] == b"%PDF":
            print(f"  {tag}✓ {len(data):,} bytes  {url!r}", flush=True)
            return data
        ct = r.headers.get("Content-Type", "")
        print(f"  {tag}not a PDF (Content-Type={ct!r})  {url!r}", flush=True)
        return None
    except Exception as exc:
        print(f"  {tag}request error: {exc}  {url!r}", flush=True)
        return None


def _extract_pdf_links(html: str, base: str = "") -> list[str]:
    """
    Extract candidate PDF links from a PMC article HTML page.
    Looks for <a href="..."> containing 'pdf' in the href or surrounding text.
    Returns de-duplicated list of absolute URLs.
    """
    seen: set[str] = set()
    links: list[str] = []

    # Pattern 1: href contains 'pdf'
    for href in re.findall(r'href=["\']([^"\']*pdf[^"\']*)["\']', html, re.I):
        url = _abs_url(href, base)
        if url and url not in seen:
            seen.add(url)
            links.append(url)

    # Pattern 2: download or related buttons with data-src / data-url
    for attr_url in re.findall(r'data-(?:src|url)=["\']([^"\']*pdf[^"\']*)["\']',
                                html, re.I):
        url = _abs_url(attr_url, base)
        if url and url not in seen:
            seen.add(url)
            links.append(url)

    # Prefer links that look like the official NCBI PDF endpoint
    links.sort(key=lambda u: (
        0 if "/pdf/" in u and "pmc.ncbi" in u else
        1 if "/pdf" in u else
        2
    ))
    return links


def _abs_url(href: str, base: str) -> str | None:
    href = href.strip()
    if not href or href.startswith(("javascript:", "mailto:", "#")):
        return None
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return None


# ── Worker URL ────────────────────────────────────────────────────────────────

def _worker_url() -> str:
    raw = os.environ.get("RENDERER_URL", "").strip().rstrip("/")
    print(f"[downloader] RENDERER_URL = {raw!r}", flush=True)
    if not raw:
        raise RuntimeError(
            "\n\n  *** RENDERER_URL is not set ***\n"
            "  Add a Replit Secret:\n"
            "    Key:   RENDERER_URL\n"
            "    Value: https://va-evidence-packet-tool-production-0817.up.railway.app\n"
        )
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    print(f"[downloader] renderer: {raw}", flush=True)
    return raw


def _check_worker(base: str) -> None:
    try:
        r = _http.get(f"{base}/health", timeout=10)
        r.raise_for_status()
        print(f"[renderer] online ✓  {base}", flush=True)
    except Exception as exc:
        raise RuntimeError(
            f"\n\n  *** Renderer not reachable at {base} ***\n"
            f"  Error: {exc}\n"
        ) from exc


# ── Batch render (Playwright via renderer worker) ─────────────────────────────

def _render_batch(base: str, urls: list[str]) -> dict[str, bytes | None]:
    """Send URLs to /render-batch; returns url → PDF bytes (None = failed)."""
    if not urls:
        return {}
    print(f"[batch] sending {len(urls)} URL(s) to /render-batch", flush=True)
    try:
        resp = _http.post(
            f"{base}/render-batch",
            json    = {"urls": urls},
            timeout = 600,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[batch] /render-batch failed: {exc} — will retry serially", flush=True)
        return {u: None for u in urls}

    out: dict[str, bytes | None] = {}
    for item in (data.get("results") or []):
        url = item.get("url", "")
        if item.get("ok") and item.get("pdf_b64"):
            try:
                pdf = base64.b64decode(item["pdf_b64"])
                if pdf[:4] == b"%PDF":
                    out[url] = pdf
                    print(f"  [batch] ✓ {len(pdf):,} bytes  {url!r}", flush=True)
                    continue
            except Exception as e:
                print(f"  [batch] decode error for {url!r}: {e}", flush=True)
        print(f"  [batch] ✗ {item.get('error','?')}  {url!r}", flush=True)
        out[url] = None

    for u in urls:
        if u not in out:
            out[u] = None

    ok = sum(1 for v in out.values() if v)
    print(f"[batch] {ok}/{len(urls)} succeeded", flush=True)
    return out


# ── Single-URL render ─────────────────────────────────────────────────────────

def _render_url(base: str, url: str, label: str = "") -> bytes | None:
    """POST one URL to /render; return PDF bytes or None."""
    tag = f"[{label}] " if label else ""
    print(f"  {tag}render  {url!r}", flush=True)
    try:
        resp = _http.post(
            f"{base}/render",
            json    = {"url": url},
            timeout = 180,
        )
    except Exception as exc:
        print(f"  {tag}request error: {exc}", flush=True)
        return None
    if resp.status_code != 200:
        print(f"  {tag}HTTP {resp.status_code}  {url!r}", flush=True)
        return None
    body = resp.content
    if body[:4] != b"%PDF":
        print(f"  {tag}not a PDF  {url!r}", flush=True)
        return None
    print(f"  {tag}✓ {len(body):,} bytes  {url!r}", flush=True)
    return body


# ── Alternate URL forms ───────────────────────────────────────────────────────

def _alternate_urls(url: str) -> list[tuple[str, str]]:
    """Return (label, alternate_url) pairs to try if the primary render fails."""
    alts: list[tuple[str, str]] = []

    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", url)
    if m:
        pmid = m.group(1)
        alts.append(("pmc-search",
                      f"https://www.ncbi.nlm.nih.gov/pmc/articles/?term={pmid}"))

    return alts


# ── Per-URL fallback ──────────────────────────────────────────────────────────

def _fallback_chain(base: str, url: str) -> tuple[bytes | None, str]:
    """
    For a URL that failed the initial pass:
      PMC: try _pmc_direct_pdf again (fresh attempt) then serial /render.
      Other: serial /render retry then alternate URL forms.
    Returns (pdf_bytes_or_None, source_label).
    NEVER generates abstract or text-only PDFs.
    """
    if _is_pmc(url):
        # One more direct-download attempt before paying for a browser
        pdf = _pmc_direct_pdf(url)
        if pdf:
            return pdf, "pmc-direct-retry"

    # Serial browser render
    pdf = _render_url(base, url, label="retry")
    if pdf:
        return pdf, "rendered-retry"

    # Alternate URL forms (browser-rendered)
    for label, alt_url in _alternate_urls(url):
        pdf = _render_url(base, alt_url, label=label)
        if pdf:
            return pdf, label

    print(f"  [fallback] all attempts exhausted  {url!r}", flush=True)
    return None, "failed"


def _safe_filename(index: int, url: str) -> str:
    m    = re.search(r"PMC(\d+)", url, re.IGNORECASE) or re.search(r"/(\d+)/?$", url)
    slug = m.group(1) if m else f"article_{index}"
    return f"{index:02d}_PMC{slug}.pdf" if re.search(r"PMC(\d+)", url, re.I) \
        else f"{index:02d}_PMID_{slug}.pdf"


# ── Public API ────────────────────────────────────────────────────────────────

async def run_downloads(
    url_illness_map: list[tuple[str, list[str]]],
    **_kwargs,
) -> dict:
    """
    Download research PDFs for all URLs.

    Flow:
      1. PMC URLs  → _pmc_direct_pdf() concurrently (fast, no browser).
      2. PMC failures + all non-PMC URLs → /render-batch (Playwright, ≤3 concurrent).
      3. Any remaining failures → _fallback_chain() concurrently.
      4. Save all results; failures recorded with no substitution.
    """
    if not url_illness_map:
        return {"downloaded": 0, "skipped": 0, "results": []}

    base    = _worker_url()
    _check_worker(base)

    urls    = [u for u, _ in url_illness_map]
    idx_map = {u: i for i, (u, _) in enumerate(url_illness_map, start=1)}
    pdf_map: dict[str, bytes | None] = {}

    pmc_urls     = [u for u in urls if _is_pmc(u)]
    non_pmc_urls = [u for u in urls if not _is_pmc(u)]

    print(f"\n[downloads] {len(urls)} URL(s) total — "
          f"{len(pmc_urls)} PMC (direct-first), "
          f"{len(non_pmc_urls)} other (browser-render)", flush=True)

    # ── Phase 1: PMC direct download (concurrent) ──────────────────────────
    if pmc_urls:
        print(f"[downloads] Phase 1: PMC direct PDF download ({len(pmc_urls)} URL(s))",
              flush=True)
        t0 = time.time()

        def _try_pmc_direct(url: str) -> tuple[str, bytes | None]:
            return url, _pmc_direct_pdf(url)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_try_pmc_direct, u): u for u in pmc_urls}
            for f in as_completed(futures):
                url, pdf = f.result()
                pdf_map[url] = pdf
                status = f"✓ {len(pdf):,}b" if pdf else "✗ (needs renderer)"
                print(f"  [pmc-direct] {status}  {url!r}", flush=True)

        pmc_ok   = sum(1 for u in pmc_urls if pdf_map.get(u))
        pmc_fail = len(pmc_urls) - pmc_ok
        print(f"[downloads] Phase 1 done in {time.time()-t0:.1f}s — "
              f"{pmc_ok} direct, {pmc_fail} need renderer", flush=True)

    # ── Phase 2: Playwright batch render (non-PMC + PMC failures) ─────────
    browser_urls = non_pmc_urls + [u for u in pmc_urls if not pdf_map.get(u)]
    if browser_urls:
        print(f"[downloads] Phase 2: browser render ({len(browser_urls)} URL(s))",
              flush=True)
        rendered = _render_batch(base, browser_urls)
        pdf_map.update(rendered)

    # ── Phase 3: fallback for anything still failed ────────────────────────
    failed_urls = [u for u in urls if not pdf_map.get(u)]
    if failed_urls:
        print(f"[downloads] Phase 3: fallback ({len(failed_urls)} URL(s))", flush=True)

        def _do_fallback(url: str) -> tuple[str, bytes | None, str]:
            pdf, src = _fallback_chain(base, url)
            return url, pdf, src

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_do_fallback, u): u for u in failed_urls}
            for f in as_completed(futures):
                url, pdf, src = f.result()
                if pdf:
                    pdf_map[url] = pdf
                    print(f"  [fallback-ok] source={src}  {url!r}", flush=True)

    # ── Phase 4: save files ────────────────────────────────────────────────
    downloaded, skipped = 0, 0
    results: list[dict] = []

    for url, folders in url_illness_map:
        i        = idx_map[url]
        pdf      = pdf_map.get(url)
        filename = _safe_filename(i, url)

        if pdf:
            saved: list[str] = []
            for folder in folders:
                Path(folder).mkdir(parents=True, exist_ok=True)
                dest = Path(folder) / filename
                dest.write_bytes(pdf)
                print(f"  [saved] {dest}  ({len(pdf):,} bytes)", flush=True)
                saved.append(str(dest))
            downloaded += 1
            results.append({"url": url, "downloaded": True,
                             "saved_paths": saved, "source": "direct"
                             if (_is_pmc(url) and pdf_map.get(url) and
                                 url not in browser_urls) else "rendered"})
        else:
            print(f"  [failed]  {url!r}", flush=True)
            skipped += 1
            results.append({"url": url, "downloaded": False,
                             "saved_paths": [], "source": "failed"})

    print(f"\n[downloads] done — {downloaded} saved, {skipped} failed", flush=True)
    return {"downloaded": downloaded, "skipped": skipped, "results": results}
