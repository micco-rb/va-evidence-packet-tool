"""
Medical research PDF downloader — external renderer worker with guaranteed fallback.

Download priority per URL:
  1. Batch render via /render-batch (all URLs in parallel, up to 3 concurrent)
  2. Per-URL fallback: DOI → PMC → abstract PDF generated locally

Sequential fallback is only triggered for URLs that fail the initial batch render.
"""

import base64
import io
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests as _http

# ── Worker URL ────────────────────────────────────────────────────────────────

def _worker_url() -> str:
    raw = os.environ.get("RENDERER_URL", "").strip().rstrip("/")
    print(f"[downloader] RENDERER_URL raw = {raw!r}", flush=True)

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


# ── Batch render ──────────────────────────────────────────────────────────────

def _render_batch(base: str, urls: list[str]) -> dict[str, bytes | None]:
    """
    Send all URLs to /render-batch (renderer handles concurrency internally).
    Returns dict mapping url → pdf_bytes (or None on failure).
    """
    print(f"[batch] sending {len(urls)} URL(s) to /render-batch", flush=True)
    try:
        resp = _http.post(
            f"{base}/render-batch",
            json    = {"urls": urls},
            timeout = 600,          # generous — renderer does 3× retries internally
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[batch] /render-batch request failed: {exc} — falling back to serial",
              flush=True)
        return {u: None for u in urls}

    out: dict[str, bytes | None] = {}
    for item in data.get("results") or []:
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

    # Any URL the batch didn't return a result for → None
    for u in urls:
        if u not in out:
            out[u] = None

    ok = sum(1 for v in out.values() if v)
    print(f"[batch] {ok}/{len(urls)} succeeded", flush=True)
    return out


# ── Single-URL render (fallback) ──────────────────────────────────────────────

def _render_url(base: str, url: str) -> bytes | None:
    """POST a single URL to /render; return PDF bytes or None."""
    try:
        resp = _http.post(
            f"{base}/render",
            json    = {"url": url},
            timeout = 180,
        )
    except Exception as exc:
        print(f"  [dl] request error: {exc}", flush=True)
        return None

    if resp.status_code != 200:
        print(f"  [dl] HTTP {resp.status_code}  {url!r}", flush=True)
        return None

    body = resp.content
    if body[:4] != b"%PDF":
        print(f"  [dl] not a PDF  {url!r}", flush=True)
        return None

    print(f"  [dl] ✓ {len(body):,} bytes  {url!r}", flush=True)
    return body


# ── NCBI eutils metadata ──────────────────────────────────────────────────────

def _get_pubmed_meta(pmid: str) -> dict:
    meta = {"title": "", "authors": "", "journal": "", "year": "",
            "abstract": "", "doi": "", "pmc_id": ""}
    try:
        url = (
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            f"?db=pubmed&id={pmid}&rettype=xml&retmode=xml"
        )
        r = _http.get(url, timeout=30, headers={"User-Agent": "VA-Evidence-Tool/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.text)

        def _text(path):
            el = root.find(path)
            return (el.text or "").strip() if el is not None else ""

        meta["title"]   = _text(".//ArticleTitle")
        meta["journal"] = _text(".//Journal/Title")
        meta["year"]    = _text(".//PubDate/Year") or _text(".//PubDate/MedlineDate")[:4]

        authors = []
        for au in root.findall(".//Author"):
            last  = _text_el(au, "LastName")
            init  = _text_el(au, "Initials")
            cname = _text_el(au, "CollectiveName")
            if last:
                authors.append(f"{last} {init}".strip())
            elif cname:
                authors.append(cname)
        meta["authors"] = ", ".join(authors)

        abstract_parts = []
        for ab in root.findall(".//Abstract/AbstractText"):
            label = ab.get("Label", "")
            text  = (ab.text or "").strip()
            if label and text:
                abstract_parts.append(f"{label}: {text}")
            elif text:
                abstract_parts.append(text)
        meta["abstract"] = " ".join(abstract_parts)

        for aid in root.findall(".//ArticleId"):
            id_type = aid.get("IdType", "")
            val     = (aid.text or "").strip()
            if id_type == "doi":
                meta["doi"] = val
            elif id_type == "pmc":
                meta["pmc_id"] = val

        print(f"  [meta] PMID {pmid}: title={meta['title'][:60]!r}  "
              f"doi={meta['doi']!r}  pmc={meta['pmc_id']!r}", flush=True)
    except Exception as exc:
        print(f"  [meta] PMID {pmid} failed: {exc}", flush=True)

    return meta


def _text_el(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _fallback_urls(pmid: str, meta: dict) -> list[str]:
    urls = []
    if meta.get("doi"):
        urls.append(f"https://doi.org/{meta['doi']}")
    pmc = meta.get("pmc_id", "")
    if pmc:
        num = pmc.replace("PMC", "").strip()
        urls.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{num}/")
    return urls


# ── Abstract PDF generator ────────────────────────────────────────────────────

def _make_abstract_pdf(pmid: str, meta: dict) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.9*inch, rightMargin=0.9*inch,
        topMargin=0.9*inch,  bottomMargin=0.9*inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("AT", parent=styles["Normal"],
        fontSize=14, leading=18, spaceAfter=8, fontName="Helvetica-Bold")
    label_style = ParagraphStyle("LB", parent=styles["Normal"],
        fontSize=9, leading=12, fontName="Helvetica-Bold",
        textColor=(0.35, 0.35, 0.35))
    value_style = ParagraphStyle("VL", parent=styles["Normal"],
        fontSize=10, leading=14, spaceAfter=6, fontName="Helvetica")
    abstract_style = ParagraphStyle("AB", parent=styles["Normal"],
        fontSize=10, leading=15, fontName="Helvetica", spaceAfter=0)
    note_style = ParagraphStyle("NT", parent=styles["Normal"],
        fontSize=8, leading=11, fontName="Helvetica-Oblique",
        textColor=(0.5, 0.5, 0.5))

    def row(label, value):
        if not value:
            return []
        return [Paragraph(label, label_style), Paragraph(value, value_style)]

    story = [Paragraph(meta["title"] or f"PubMed Article {pmid}", title_style),
             Spacer(1, 0.1*inch)]
    for lbl, val in [
        ("Authors",    meta["authors"]),
        ("Journal",    meta["journal"]),
        ("Year",       meta["year"]),
        ("PMID",       pmid),
        ("DOI",        meta["doi"]),
        ("PubMed URL", f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"),
    ]:
        story.extend(row(lbl, val))

    if meta["abstract"]:
        story += [Spacer(1, 0.15*inch), Paragraph("Abstract", label_style),
                  Spacer(1, 0.04*inch), Paragraph(meta["abstract"], abstract_style)]

    story += [Spacer(1, 0.25*inch), Paragraph(
        "Note: Full-text browser PDF could not be rendered. "
        "This abstract was retrieved from PubMed and is provided for reference.",
        note_style,
    )]
    doc.build(story)
    pdf_bytes = buf.getvalue()
    print(f"  [abstract-pdf] {len(pdf_bytes):,} bytes  PMID {pmid}", flush=True)
    return pdf_bytes


# ── Per-URL fallback chain (used after batch failures) ────────────────────────

def _fallback_chain(base: str, url: str) -> tuple[bytes | None, str]:
    """
    For a URL that failed the batch render, try:
      1. Serial /render of the original URL
      2. DOI / PMC alternate URLs
      3. Abstract PDF from NCBI metadata
    """
    m    = re.search(r"/(\d+)/?$", url)
    pmid = m.group(1) if m else None

    # Step 1: retry via serial /render (fresh single attempt)
    print(f"  [fallback] serial render  {url!r}", flush=True)
    pdf = _render_url(base, url)
    if pdf:
        return pdf, "pubmed"

    if not pmid:
        return None, "failed"

    # Step 2: metadata + alternate URLs
    meta     = _get_pubmed_meta(pmid)
    alt_urls = _fallback_urls(pmid, meta)
    for label, alt_url in zip(("doi", "pmc"), alt_urls):
        print(f"  [fallback] {label.upper()}  {alt_url!r}", flush=True)
        pdf = _render_url(base, alt_url)
        if pdf:
            return pdf, label

    # Step 3: abstract PDF
    if meta["title"] or meta["abstract"]:
        try:
            return _make_abstract_pdf(pmid, meta), "abstract"
        except Exception as exc:
            print(f"  [fallback] abstract PDF failed: {exc}", flush=True)

    return None, "failed"


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
    Download research PDFs for all URLs, saving each to its target folders.

    Flow:
      1. Send ALL URLs to /render-batch in one shot (renderer runs ≤3 concurrently).
      2. For any URL that failed, run the fallback chain concurrently
         (ThreadPoolExecutor, max 3 workers).
      3. Save results and return summary.

    url_illness_map: list of (url, [folder_path, ...])
    """
    if not url_illness_map:
        return {"downloaded": 0, "skipped": 0, "results": []}

    base = _worker_url()
    _check_worker(base)

    urls     = [u for u, _ in url_illness_map]
    url_map  = {u: folders for u, folders in url_illness_map}
    idx_map  = {u: i for i, (u, _) in enumerate(url_illness_map, start=1)}

    print(f"\n[downloads] {len(urls)} URL(s) total", flush=True)

    # ── Phase 1: batch render ──────────────────────────────────────────────
    batch_results = _render_batch(base, urls)

    # ── Phase 2: fallback for failures ────────────────────────────────────
    failed_urls = [u for u in urls if not batch_results.get(u)]
    if failed_urls:
        print(f"[downloads] {len(failed_urls)} URL(s) need fallback", flush=True)

        def _run_fallback(url: str) -> tuple[str, bytes | None, str]:
            pdf, src = _fallback_chain(base, url)
            return url, pdf, src

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_run_fallback, u): u for u in failed_urls}
            for f in as_completed(futures):
                url, pdf, src = f.result()
                if pdf:
                    batch_results[url] = pdf
                    print(f"  [fallback-ok] {src}  {url!r}", flush=True)

    # ── Phase 3: save files ────────────────────────────────────────────────
    downloaded, skipped = 0, 0
    results: list[dict] = []

    for url, folders in url_illness_map:
        i        = idx_map[url]
        pdf      = batch_results.get(url)
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
                            "saved_paths": saved, "source": "ok"})
        else:
            print(f"  [skip] all fallbacks exhausted  {url!r}", flush=True)
            skipped += 1
            results.append({"url": url, "downloaded": False,
                            "saved_paths": [], "source": "failed"})

    print(f"\n[downloads] done — {downloaded} saved, {skipped} failed", flush=True)
    return {"downloaded": downloaded, "skipped": skipped, "results": results}
