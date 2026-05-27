"""
Medical research PDF downloader — external renderer worker, browser-render only.

Every URL is rendered via Playwright (page.pdf()). No abstract extraction,
no text fallback, no transcription PDFs — ever.

Download flow:
  1. /render-batch: all URLs sent at once, renderer runs ≤3 concurrently.
  2. Fallback for failures: retry same URL via /render (fresh attempt).
  3. Fallback: try alternate URL forms (DOI redirect, PMC full-text page).
  4. Still failed → marked failed. No abstract PDF generated.
"""

import base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests as _http


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


# ── Batch render (primary path) ───────────────────────────────────────────────

def _render_batch(base: str, urls: list[str]) -> dict[str, bytes | None]:
    """
    Send all URLs to /render-batch (renderer handles ≤3 concurrent internally).
    Returns dict mapping url → PDF bytes (None = render failed).
    """
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


# ── Alternate URL forms for a PubMed URL ─────────────────────────────────────

def _alternate_urls(url: str) -> list[tuple[str, str]]:
    """
    Given any URL, return (label, alternate_url) pairs to try if the primary fails.
    Uses only URL transforms — no NCBI API calls, no metadata extraction.
    """
    alts: list[tuple[str, str]] = []

    # PubMed article page → PMC full-text (if PMID is in URL)
    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", url)
    if m:
        pmid = m.group(1)
        alts.append(("pmc-search",
                      f"https://www.ncbi.nlm.nih.gov/pmc/articles/?term={pmid}"))

    # doi.org redirect — strip and try the canonical PubMed URL
    # (noop for now; can be extended if needed)
    return alts


# ── Per-URL fallback (browser-only, no abstract generation) ──────────────────

def _fallback_chain(base: str, url: str) -> bytes | None:
    """
    For a URL that failed the initial batch render:
      1. Retry the same URL via serial /render (fresh renderer attempt).
      2. Try alternate URL forms (URL transforms only — no NCBI API).
    Returns PDF bytes if any attempt succeeds, None otherwise.
    NEVER generates abstract or text-only PDFs.
    """
    # Retry #1: serial /render of the original URL
    pdf = _render_url(base, url, label="retry")
    if pdf:
        return pdf

    # Retry #2: alternate URL forms (browser-rendered)
    for label, alt_url in _alternate_urls(url):
        pdf = _render_url(base, alt_url, label=label)
        if pdf:
            return pdf

    print(f"  [fallback] all browser-render attempts exhausted  {url!r}", flush=True)
    return None


def _safe_filename(index: int, url: str) -> str:
    m    = re.search(r"/(\d+)/?$", url)
    pmid = m.group(1) if m else f"article_{index}"
    return f"{index:02d}_PMID_{pmid}.pdf"


# ── Public API ────────────────────────────────────────────────────────────────

async def run_downloads(
    url_illness_map: list[tuple[str, list[str]]],
    **_kwargs,
) -> dict:
    """
    Download research PDFs for all URLs via full browser render only.

    Flow:
      1. /render-batch: all URLs in one shot (renderer concurrency ≤3).
      2. Fallback: failed URLs retried concurrently (ThreadPoolExecutor, 3 workers).
         Fallback is browser-render only — no abstract PDF generation.
      3. Save results; failed URLs are recorded as failed (not substituted).

    url_illness_map: list of (url, [folder_path, ...])
    """
    if not url_illness_map:
        return {"downloaded": 0, "skipped": 0, "results": []}

    base    = _worker_url()
    _check_worker(base)

    urls    = [u for u, _ in url_illness_map]
    idx_map = {u: i for i, (u, _) in enumerate(url_illness_map, start=1)}

    print(f"\n[downloads] {len(urls)} URL(s) total", flush=True)

    # ── Phase 1: batch render ──────────────────────────────────────────────
    pdf_map = _render_batch(base, urls)

    # ── Phase 2: concurrent fallback for failures ──────────────────────────
    failed_urls = [u for u in urls if not pdf_map.get(u)]
    if failed_urls:
        print(f"[downloads] {len(failed_urls)} URL(s) need fallback", flush=True)

        def _do_fallback(url: str) -> tuple[str, bytes | None]:
            return url, _fallback_chain(base, url)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_do_fallback, u): u for u in failed_urls}
            for f in as_completed(futures):
                url, pdf = f.result()
                if pdf:
                    pdf_map[url] = pdf
                    print(f"  [fallback-ok]  {url!r}", flush=True)

    # ── Phase 3: save files ────────────────────────────────────────────────
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
                            "saved_paths": saved, "source": "rendered"})
        else:
            print(f"  [failed] all browser-render attempts failed  {url!r}", flush=True)
            skipped += 1
            results.append({"url": url, "downloaded": False,
                            "saved_paths": [], "source": "failed"})

    print(f"\n[downloads] done — {downloaded} saved, {skipped} failed", flush=True)
    return {"downloaded": downloaded, "skipped": skipped, "results": results}
