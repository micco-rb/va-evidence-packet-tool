"""
Medical research PDF downloader — external renderer worker with guaranteed fallback.

Download priority per PMID:
  1. Full PubMed browser PDF (Playwright renderer)
  2. DOI landing page PDF   (Playwright renderer)
  3. PMC full-text PDF      (Playwright renderer)
  4. Printable abstract PDF (generated locally from NCBI eutils metadata)

"Paywalled / No PDF" is NEVER shown unless the PMID page itself is unreachable.
"""

import io
import os
import re
import time
import xml.etree.ElementTree as ET
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

    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
        print(f"[downloader] scheme missing — normalized to: {raw!r}", flush=True)

    print(f"[downloader] Using renderer at: {raw}", flush=True)
    return raw


def _check_worker(base: str) -> None:
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
            timeout = 360,
        )
    except _http.exceptions.ConnectionError as exc:
        print(f"  [dl] CONNECTION ERROR: {exc}", flush=True)
        return None
    except _http.exceptions.Timeout:
        print(f"  [dl] TIMEOUT for {url}", flush=True)
        return None
    except Exception as exc:
        print(f"  [dl] UNEXPECTED REQUEST ERROR: {type(exc).__name__}: {exc}", flush=True)
        return None

    print(f"  [dl] HTTP status   : {resp.status_code}", flush=True)
    ct = resp.headers.get("Content-Type", "<none>")
    print(f"  [dl] Content-Type  : {ct}", flush=True)
    body = resp.content
    print(f"  [dl] Response size : {len(body):,} bytes", flush=True)
    print(f"  [dl] First 8 bytes : {body[:8]!r}", flush=True)

    if resp.status_code != 200:
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


# ── NCBI eutils metadata ──────────────────────────────────────────────────────

def _get_pubmed_meta(pmid: str) -> dict:
    """
    Fetch article metadata from NCBI eutils.
    Returns dict with keys: title, authors, journal, year, abstract, doi, pmc_id.
    All values are strings (empty string if unavailable).
    """
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

        # Authors — "Last FM, Last FM, ..."
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

        # Abstract — may have multiple AbstractText elements with labels
        abstract_parts = []
        for ab in root.findall(".//Abstract/AbstractText"):
            label = ab.get("Label", "")
            text  = (ab.text or "").strip()
            if label and text:
                abstract_parts.append(f"{label}: {text}")
            elif text:
                abstract_parts.append(text)
        meta["abstract"] = " ".join(abstract_parts)

        # IDs
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
        print(f"  [meta] PMID {pmid} fetch failed: {exc}", flush=True)

    return meta


def _text_el(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


# ── Fallback URL builder ──────────────────────────────────────────────────────

def _fallback_urls(pmid: str, meta: dict) -> list[str]:
    """
    Build ordered list of alternative URLs to attempt rendering.
    Priority: DOI landing page → PMC full-text → direct publisher.
    """
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
    """
    Generate a clean, printable abstract PDF from NCBI metadata.
    Uses reportlab (already installed). Returns PDF bytes.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize    = letter,
        leftMargin  = 0.9 * inch,
        rightMargin = 0.9 * inch,
        topMargin   = 0.9 * inch,
        bottomMargin= 0.9 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ArticleTitle",
        parent    = styles["Normal"],
        fontSize  = 14,
        leading   = 18,
        spaceAfter= 8,
        fontName  = "Helvetica-Bold",
    )
    label_style = ParagraphStyle(
        "Label",
        parent    = styles["Normal"],
        fontSize  = 9,
        leading   = 12,
        fontName  = "Helvetica-Bold",
        textColor = (0.35, 0.35, 0.35),
    )
    value_style = ParagraphStyle(
        "Value",
        parent    = styles["Normal"],
        fontSize  = 10,
        leading   = 14,
        spaceAfter= 6,
        fontName  = "Helvetica",
    )
    abstract_style = ParagraphStyle(
        "Abstract",
        parent    = styles["Normal"],
        fontSize  = 10,
        leading   = 15,
        fontName  = "Helvetica",
        spaceAfter= 0,
    )
    note_style = ParagraphStyle(
        "Note",
        parent    = styles["Normal"],
        fontSize  = 8,
        leading   = 11,
        fontName  = "Helvetica-Oblique",
        textColor = (0.5, 0.5, 0.5),
    )

    def row(label, value):
        if not value:
            return []
        return [
            Paragraph(label, label_style),
            Paragraph(value, value_style),
        ]

    pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

    story = []
    story.append(Paragraph(
        meta["title"] or f"PubMed Article {pmid}",
        title_style,
    ))
    story.append(Spacer(1, 0.1 * inch))

    for lbl, val in [
        ("Authors",        meta["authors"]),
        ("Journal",        meta["journal"]),
        ("Year",           meta["year"]),
        ("PMID",           pmid),
        ("DOI",            meta["doi"]),
        ("PubMed URL",     pubmed_url),
    ]:
        story.extend(row(lbl, val))

    if meta["abstract"]:
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph("Abstract", label_style))
        story.append(Spacer(1, 0.04 * inch))
        story.append(Paragraph(meta["abstract"], abstract_style))

    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph(
        "Note: Full-text browser PDF could not be rendered. "
        "This abstract was retrieved from PubMed and is provided for reference.",
        note_style,
    ))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    print(f"  [abstract-pdf] Generated {len(pdf_bytes):,}-byte abstract PDF "
          f"for PMID {pmid}", flush=True)
    return pdf_bytes


# ── Filename helper ───────────────────────────────────────────────────────────

def _safe_filename(index: int, url: str) -> str:
    m = re.search(r"/(\d+)/?$", url)
    pmid = m.group(1) if m else f"article_{index}"
    return f"{index:02d}_PMID_{pmid}.pdf"


# ── Guaranteed-download with fallback chain ───────────────────────────────────

def _download_with_fallback(base: str, url: str) -> tuple[bytes | None, str]:
    """
    Try every fallback path before giving up.
    Returns (pdf_bytes, source_label).
    source_label is one of: 'pubmed', 'doi', 'pmc', 'abstract', 'failed'.
    """
    # Extract PMID from URL
    m = re.search(r"/(\d+)/?$", url)
    pmid = m.group(1) if m else None

    # ── Step 1: PubMed Playwright ─────────────────────────────────────────
    print(f"  [fallback] Step 1 — PubMed renderer  url={url!r}", flush=True)
    pdf = _render_url(base, url)
    if pdf:
        return pdf, "pubmed"

    if not pmid:
        print(f"  [fallback] Cannot extract PMID from {url!r} — no fallback possible",
              flush=True)
        return None, "failed"

    # ── Step 2: Fetch metadata ────────────────────────────────────────────
    print(f"  [fallback] Step 2 — Fetching NCBI metadata for PMID {pmid}", flush=True)
    meta = _get_pubmed_meta(pmid)

    # ── Step 3: DOI / PMC renderer ────────────────────────────────────────
    alt_urls = _fallback_urls(pmid, meta)
    for label, alt_url in zip(("doi", "pmc"), alt_urls):
        print(f"  [fallback] Step 3 — {label.upper()} renderer  url={alt_url!r}",
              flush=True)
        pdf = _render_url(base, alt_url)
        if pdf:
            return pdf, label

    # ── Step 4: Abstract PDF ──────────────────────────────────────────────
    if meta["title"] or meta["abstract"]:
        print(f"  [fallback] Step 4 — Generating abstract PDF for PMID {pmid}",
              flush=True)
        try:
            pdf = _make_abstract_pdf(pmid, meta)
            return pdf, "abstract"
        except Exception as exc:
            print(f"  [fallback] Abstract PDF generation failed: {exc}", flush=True)

    print(f"  [fallback] All paths exhausted for PMID {pmid}", flush=True)
    return None, "failed"


# ── Public API ────────────────────────────────────────────────────────────────

async def run_downloads(
    url_illness_map: list[tuple[str, list[str]]],
    **_kwargs,
) -> dict:
    """
    Send each article URL through the guaranteed fallback chain and save the
    resulting PDF to every target folder.

    url_illness_map: list of (url, [folder_path, ...])
    Returns {'downloaded': int, 'skipped': int, 'results': list[dict]}.
    """
    base = _worker_url()
    _check_worker(base)
    print(f"[renderer] Rendering {len(url_illness_map)} article(s) via external worker",
          flush=True)

    downloaded, skipped = 0, 0
    results: list[dict] = []

    for i, (url, folders) in enumerate(url_illness_map, start=1):
        print(f"\n[{i}/{len(url_illness_map)}] {url}", flush=True)
        pdf_bytes, source = _download_with_fallback(base, url)

        if pdf_bytes:
            filename = _safe_filename(i, url)
            saved: list[str] = []
            for folder in folders:
                Path(folder).mkdir(parents=True, exist_ok=True)
                dest = Path(folder) / filename
                dest.write_bytes(pdf_bytes)
                print(f"  [saved] {dest}  ({len(pdf_bytes):,} bytes)  "
                      f"source={source}", flush=True)
                saved.append(str(dest))
            downloaded += 1
            results.append({
                "url": url, "downloaded": True,
                "saved_paths": saved, "source": source,
            })
        else:
            print(f"  [skip] All fallbacks exhausted — article omitted", flush=True)
            skipped += 1
            results.append({
                "url": url, "downloaded": False,
                "saved_paths": [], "source": "failed",
            })

        if i < len(url_illness_map):
            time.sleep(0.3)

    print(f"\n[renderer] Done — {downloaded} rendered, {skipped} failed", flush=True)
    return {"downloaded": downloaded, "skipped": skipped, "results": results}
