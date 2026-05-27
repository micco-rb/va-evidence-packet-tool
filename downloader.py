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
    raw = os.environ.get("RENDERER_URL", "").strip().rstrip("/")
    print(f"[downloader] RENDERER_URL raw = {raw!r}", flush=True)

    if not raw:
        raise RuntimeError(
            "\n\n  *** RENDERER_URL is not set ***\n"
            "  Add a Replit Secret with:\n"
            "    Key:   RENDERER_URL\n"
            "    Value: https://va-evidence-packet-tool-production-0817.up.railway.app\n"
        )

    # Auto-add https:// if the value was saved without a scheme
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
        print(f"[downloader] scheme missing — normalized to: {raw!r}", flush=True)

    print(f"[downloader] Using renderer at: {raw}", flush=True)
    return raw


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
    endpoint = f"{base}/render"
    print(f"  [dl] POST {endpoint}", flush=True)
    print(f"  [dl] payload: url={url!r}", flush=True)

    try:
        resp = _http.post(
            endpoint,
            json    = {"url": url},
            timeout = 120,                  # generous — page load + PDF render
        )
    except _http.exceptions.ConnectionError as exc:
        print(f"  [dl] CONNECTION ERROR: {exc}", flush=True)
        return None
    except _http.exceptions.Timeout:
        print(f"  [dl] TIMEOUT after 120 s for {url}", flush=True)
        return None
    except Exception as exc:
        print(f"  [dl] UNEXPECTED REQUEST ERROR: {type(exc).__name__}: {exc}", flush=True)
        return None

    # ── Step 1: HTTP status ───────────────────────────────────────────────
    print(f"  [dl] HTTP status   : {resp.status_code}", flush=True)

    # ── Step 2: Content-Type ──────────────────────────────────────────────
    ct = resp.headers.get("Content-Type", "<none>")
    print(f"  [dl] Content-Type  : {ct}", flush=True)

    # ── Step 3: Byte size ─────────────────────────────────────────────────
    body = resp.content
    print(f"  [dl] Response size : {len(body):,} bytes", flush=True)

    # ── Step 4: First bytes (magic number check) ──────────────────────────
    print(f"  [dl] First 8 bytes : {body[:8]!r}", flush=True)

    if resp.status_code != 200:
        # Print full error body (up to 500 chars) for diagnosis
        try:
            err_text = resp.json()
        except Exception:
            err_text = body[:500].decode("utf-8", errors="replace")
        print(f"  [dl] WORKER ERROR body: {err_text}", flush=True)
        return None

    if body[:4] != b"%PDF":
        print(f"  [dl] NOT A VALID PDF — first bytes are {body[:20]!r}", flush=True)
        print(f"  [dl] Body preview (first 300 chars): {body[:300].decode('utf-8', errors='replace')!r}", flush=True)
        return None

    size = int(resp.headers.get("X-PDF-Size", len(body)))
    print(f"  [dl] PDF OK — {size:,} bytes  ✓", flush=True)
    return body


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
