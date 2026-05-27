"""
Medical research PDF downloader — external renderer worker.

Posts each article URL to the renderer worker (RENDERER_URL env var).
The worker runs Playwright/Chromium on your local machine and returns
a real browser-print PDF.

There is NO inline Playwright, NO WeasyPrint, NO fallback rendering.
If RENDERER_URL is not set or the worker is unreachable, the download
fails loudly so the problem is immediately visible in the logs.

Setup:
  1. cd renderer_worker && ./start.sh   (or start.bat on Windows)
  2. Add Replit Secret:  RENDERER_URL = http://<your-LAN-ip>:7777
"""

import os
import re
import time
from pathlib import Path

import requests as _http

# ── Worker URL ────────────────────────────────────────────────────────────────

def _worker_url() -> str:
    url = os.environ.get("RENDERER_URL", "").rstrip("/")
    if not url:
        raise RuntimeError(
            "\n\n  *** RENDERER_URL is not set ***\n"
            "  Start the local renderer worker and add the secret:\n"
            "    Key:   RENDERER_URL\n"
            "    Value: http://<your-LAN-ip>:7777\n"
            "  See renderer_worker/README.md for setup instructions.\n"
        )
    return url


def _check_worker(base: str) -> None:
    """Confirm the worker is reachable before starting a batch."""
    try:
        r = _http.get(f"{base}/health", timeout=5)
        r.raise_for_status()
        print(f"[renderer] Worker online at {base}  ✓", flush=True)
    except Exception as exc:
        raise RuntimeError(
            f"\n\n  *** Renderer worker not reachable at {base} ***\n"
            f"  Error: {exc}\n"
            "  Make sure the worker is running:  cd renderer_worker && ./start.sh\n"
        ) from exc


# ── Per-URL render call ───────────────────────────────────────────────────────

def _render_url(base: str, url: str) -> bytes | None:
    """POST url to the worker; return PDF bytes or None on failure."""
    try:
        resp = _http.post(
            f"{base}/render",
            json    = {"url": url},
            timeout = 90,                   # generous — page load + PDF render
        )
    except _http.exceptions.ConnectionError as exc:
        print(f"  [renderer] Connection lost: {exc}", flush=True)
        return None
    except _http.exceptions.Timeout:
        print(f"  [renderer] Request timed out for {url}", flush=True)
        return None

    if resp.status_code != 200:
        print(f"  [renderer] Worker error {resp.status_code}: {resp.text[:200]}", flush=True)
        return None

    pdf = resp.content
    if pdf[:4] != b"%PDF":
        print(f"  [renderer] Response is not a PDF ({len(pdf)} bytes) — skipping", flush=True)
        return None

    size = int(resp.headers.get("X-PDF-Size", len(pdf)))
    print(f"  [renderer] Received {size:,} bytes  ✓", flush=True)
    return pdf


# ── Filename helper ───────────────────────────────────────────────────────────

def _safe_filename(index: int, url: str) -> str:
    m = re.search(r"/(\d+)/?$", url)
    pmid = m.group(1) if m else f"article_{index}"
    return f"{index:02d}_PMID_{pmid}.pdf"


# ── Public API ────────────────────────────────────────────────────────────────

async def run_downloads(
    url_illness_map: list[tuple[str, list[str]]],
    **_kwargs,
) -> dict:
    """
    Send each article URL to the renderer worker and save the resulting PDF
    to every target folder in its list.

    url_illness_map: list of (url, [folder_path, ...])
    Returns {'downloaded': int, 'skipped': int, 'results': list[dict]}.
    Raises RuntimeError immediately if RENDERER_URL is not set or the worker
    is not reachable.
    """
    base = _worker_url()
    _check_worker(base)
    print(f"[renderer] Rendering {len(url_illness_map)} article(s) via external worker", flush=True)

    downloaded, skipped = 0, 0
    results: list[dict] = []

    for i, (url, folders) in enumerate(url_illness_map, start=1):
        print(f"\n[{i}/{len(url_illness_map)}] {url}", flush=True)
        pdf_bytes = _render_url(base, url)

        if pdf_bytes:
            filename = _safe_filename(i, url)
            for folder in folders:
                Path(folder).mkdir(parents=True, exist_ok=True)
                dest = Path(folder) / filename
                dest.write_bytes(pdf_bytes)
                print(f"  [saved] {dest}  ({len(pdf_bytes):,} bytes)", flush=True)
            downloaded += 1
            results.append({"url": url, "downloaded": True})
        else:
            print(f"  [skip] No PDF returned — article omitted from packet", flush=True)
            skipped += 1
            results.append({"url": url, "downloaded": False})

        if i < len(url_illness_map):
            time.sleep(0.3)

    print(f"\n[renderer] Done — {downloaded} rendered, {skipped} failed", flush=True)
    return {"downloaded": downloaded, "skipped": skipped, "results": results}
