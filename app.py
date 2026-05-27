"""
Flask web UI — VA Medical Evidence Packet Automation.

Pipeline (per condition):
  1. POST /upload            — parse memo; extract conditions, vet name, VA file #
  2. POST /upload-esf/<id>   — attach ESF PDF
  3. POST /process/<id>      — confirm conditions → background pipeline:
                                download → per-condition ESF fill → merge → headers
  4. GET  /status/<id>       — poll progress
  5. GET  /final/<id>/<cond> — download Final_<Condition>_Packet.pdf
  6. GET  /file/<id>/<rel>   — serve individual research PDF
"""

import asyncio
import io
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

import downloader
import esf_filler
from main import (
    create_folders,
    extract_docx_text,
    extract_illnesses_smart,
    extract_pdf_text,
    extract_va_file_number,
    extract_vet_name,
    extract_urls,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

UPLOAD_DIR    = Path("uploads")
OUTPUT_DIR    = Path("output")
SIGNATURE_DIR = Path("static") / "signatures"
SIGNATURE_PATH = SIGNATURE_DIR / "jeff_signature.png"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
SIGNATURE_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _pubmed_urls(text: str) -> list[str]:
    return [u for u in extract_urls(text) if "pubmed.ncbi.nlm.nih.gov" in u]


def _nearest_condition(url: str, text: str, confirmed: list[str]) -> str | None:
    """
    Return whichever confirmed condition name appears closest (by character
    distance) to this URL's position in the text, within a 3000-char radius.

    Returns None if no condition name is found within the radius.
    """
    idx = text.find(url)
    if idx == -1:
        return None
    text_lower  = text.lower()
    best_cond   = None
    best_dist   = float("inf")
    for cond in confirmed:
        cond_lower = cond.lower()
        pos = 0
        while True:
            p = text_lower.find(cond_lower, pos)
            if p == -1:
                break
            d = abs(p - idx)
            if d < best_dist:
                best_dist, best_cond = d, cond
            pos = p + 1
    return best_cond if best_dist <= 3000 else None


def _cond_slug(condition: str) -> str:
    return re.sub(r"[^\w]", "_", condition).strip("_")


# ─────────────────────────────────────────────────────────────────────────
# In-memory job store
# ─────────────────────────────────────────────────────────────────────────

jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _job(job_id: str) -> dict:
    with _lock:
        return dict(jobs.get(job_id, {}))


def _update(job_id: str, **kw):
    with _lock:
        if job_id in jobs:
            jobs[job_id].update(kw)


def _log(job_id: str, line: str):
    print(line)
    with _lock:
        if job_id in jobs:
            jobs[job_id]["logs"].append(line)


# ─────────────────────────────────────────────────────────────────────────
# Background pipeline  (per-condition packets)
# ─────────────────────────────────────────────────────────────────────────

def _pipeline(job_id: str, confirmed: list[str]):
    def log(msg: str):
        _log(job_id, msg)

    try:
        j              = _job(job_id)
        vet_name       = j["vet_name"]
        va_file_number = j.get("va_file_number", "")
        text           = j["text"]
        esf_path       = j.get("esf_path")
        urls           = j["urls"]

        log(f"Veteran  : {vet_name}")
        log(f"VA File# : {va_file_number or '(not found)'}")
        log(f"Conditions: {', '.join(confirmed)}")
        log(f"PubMed articles: {len(urls)}")

        # ── Stage 1: download all research PDFs ───────────────────────
        _update(job_id, stage="downloading")

        # Each job gets its own isolated download directory under output/<job_id>/.
        # This prevents any cross-run or cross-condition contamination — a previous
        # run's leftover PDFs can never bleed into the current run.
        job_dl_dir = OUTPUT_DIR / job_id / "research"
        job_dl_dir.mkdir(parents=True, exist_ok=True)

        # Pre-create one subfolder per condition (slug-safe names).
        cond_folders: dict[str, Path] = {}
        for cond in confirmed:
            slug_dir = job_dl_dir / _cond_slug(cond)
            slug_dir.mkdir(exist_ok=True)
            cond_folders[cond] = slug_dir

        # Assign each URL to exactly one condition — the nearest one in the text.
        # If a URL cannot be matched to any condition (extremely rare), assign it
        # to the first confirmed condition rather than broadcasting to all of them.
        # Use a list (not just a dict) so duplicate URLs are each processed once.
        seen_urls: set[str] = set()
        url_to_cond: dict[str, str] = {}
        url_map: list[tuple[str, list[str]]] = []
        for url in urls:
            if url in seen_urls:
                continue          # skip exact duplicates — one download per URL
            seen_urls.add(url)
            cond = _nearest_condition(url, text, confirmed) or confirmed[0]
            url_to_cond[url] = cond
            url_map.append((url, [str(cond_folders[cond])]))
            log(f"  {url} → {cond}")

        log(f"\nDownloading {len(url_map)} article(s)…")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            dl_results = loop.run_until_complete(downloader.run_downloads(url_map))
        finally:
            loop.close()

        log(f"Downloaded: {dl_results['downloaded']}  |  Skipped: {dl_results['skipped']}")

        # Build per-condition PDF lists by scanning ONLY each condition's isolated
        # subfolder.  Because every folder is job-scoped and condition-scoped, a
        # PDF from Sleep Apnea can never appear in the Hypertension list.
        illness_files: dict[str, list[str]] = {}
        for cond in confirmed:
            folder = cond_folders[cond]
            illness_files[cond] = (
                sorted(str(p) for p in folder.glob("*.pdf"))
            )

        article_results: list[dict] = []
        for entry in dl_results["results"]:
            m    = re.search(r"/(\d+)/?$", entry["url"])
            pmid = m.group(1) if m else ""
            article_results.append({
                "url":        entry["url"],
                "downloaded": entry["downloaded"],
                "pmid":       pmid,
                "filename":   f"PMID_{pmid}.pdf" if pmid else "article.pdf",
            })

        # ── Stage 2 & 3: per-condition ESF fill + merge + headers ─────
        condition_packets: dict[str, str] = {}

        if esf_path and Path(esf_path).exists():
            for cond in confirmed:
                slug        = _cond_slug(cond)
                cond_pdfs    = illness_files.get(cond, [])
                # Only include articles that were downloaded FOR this condition
                cond_entries = [
                    a for a in article_results
                    if a["downloaded"] and url_to_cond.get(a["url"]) == cond
                ]

                # Fill ESF
                _update(job_id, stage="filling_esf",
                        current_condition=cond)
                log(f"\n[{cond}] Filling ESF…")
                filled_esf = str(OUTPUT_DIR / f"{job_id}_{slug}_ESF.pdf")
                esf_filler.fill_esf_for_condition(
                    esf_path         = esf_path,
                    output_path      = filled_esf,
                    condition        = cond,
                    research_entries = cond_entries,
                )

                # Stamp Jeff signature + today's date on Section VI
                log(f"[{cond}] Applying signature/date…")
                esf_filler.apply_signature_and_date(
                    esf_path    = filled_esf,
                    output_path = filled_esf,
                )

                # Merge + add headers
                _update(job_id, stage="merging")
                log(f"[{cond}] Merging {len(cond_pdfs)} research PDF(s)…")
                packet_path = str(OUTPUT_DIR / f"{job_id}_Final_{slug}_Packet.pdf")
                esf_filler.build_condition_packet(
                    filled_esf_path    = filled_esf,
                    research_pdf_paths = cond_pdfs,
                    output_path        = packet_path,
                    vet_name           = vet_name,
                    va_file_number     = va_file_number,
                    condition          = cond,
                )
                condition_packets[cond] = packet_path
                log(f"[{cond}] → {Path(packet_path).name}  ✓")

        else:
            log("\nNo ESF provided — research PDFs saved per condition.")

        _update(
            job_id,
            status             = "done",
            stage              = "done",
            illnesses          = confirmed,
            dl_summary         = dl_results,
            article_results    = article_results,
            illness_files      = illness_files,
            condition_packets  = condition_packets,
            current_condition  = "",
        )
        log("\nAll packets complete.")

    except Exception as exc:
        import traceback
        _update(job_id, status="error", error=str(exc))
        _log(job_id, f"ERROR: {exc}")
        _log(job_id, traceback.format_exc())


# ─────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.ico", mimetype="image/x-icon")


# ── Admin: signature management ───────────────────────────────────────────

@app.route("/admin/signature-status")
def signature_status():
    exists = SIGNATURE_PATH.exists()
    return jsonify(exists=exists)


@app.route("/admin/signature", methods=["POST"])
def upload_signature():
    if "file" not in request.files:
        return jsonify(error="No file provided"), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify(error="No filename"), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return jsonify(error="Image files only (.png .jpg .gif .webp)"), 400
    SIGNATURE_DIR.mkdir(parents=True, exist_ok=True)
    # Always save as PNG name regardless of upload format
    SIGNATURE_PATH.write_bytes(f.read())
    return jsonify(ok=True)


@app.route("/admin/signature", methods=["DELETE"])
def delete_signature():
    if SIGNATURE_PATH.exists():
        SIGNATURE_PATH.unlink()
    return jsonify(ok=True)


@app.route("/")
def index():
    return render_template("index.html")


# ── Phase 1: upload + parse memo ─────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify(error="No file provided"), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify(error="Empty filename"), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in (".docx", ".pdf"):
        return jsonify(error="Only .docx and .pdf files are supported"), 400

    job_id    = str(uuid.uuid4())[:8]
    save_path = str(UPLOAD_DIR / f"{job_id}_memo{ext}")
    f.save(save_path)

    try:
        text           = extract_docx_text(save_path) if ext == ".docx" else extract_pdf_text(save_path)
        vet_name       = extract_vet_name(text)
        va_file_number = extract_va_file_number(text)
        extraction     = extract_illnesses_smart(text)
        urls           = _pubmed_urls(text)
    except Exception as exc:
        return jsonify(error=f"Could not parse document: {exc}"), 500

    with _lock:
        jobs[job_id] = {
            "id":               job_id,
            "filename":         f.filename,
            "file_path":        save_path,
            "esf_path":         None,
            "esf_filename":     None,
            "text":             text,
            "status":           "parsed",
            "stage":            "confirm",
            "vet_name":         vet_name,
            "va_file_number":   va_file_number,
            "illnesses_clean":  extraction["illnesses"],
            "illnesses":        extraction["illnesses"],
            "extraction_debug": extraction["debug_log"],
            "urls":             urls,
            "logs":             [],
            "article_results":  [],
            "illness_files":    {},
            "dl_summary":       {},
            "condition_packets": {},
            "current_condition": "",
            "error":            None,
            "created":          time.time(),
        }

    return jsonify(
        job_id           = job_id,
        vet_name         = vet_name,
        va_file_number   = va_file_number,
        illnesses_clean  = extraction["illnesses"],
        extraction_debug = extraction["debug_log"],
        url_count        = len(urls),
    )


# ── Phase 1b: upload ESF ─────────────────────────────────────────────────

@app.route("/upload-esf/<job_id>", methods=["POST"])
def upload_esf(job_id: str):
    if not _job(job_id):
        return jsonify(error="Job not found"), 404

    if "file" not in request.files:
        return jsonify(error="No file provided"), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify(error="Empty filename"), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify(error="ESF must be a PDF file"), 400

    save_path = str(UPLOAD_DIR / f"{job_id}_esf.pdf")
    f.save(save_path)

    # Also try to extract VA file number from ESF text if memo didn't have one
    j = _job(job_id)
    if not j.get("va_file_number"):
        try:
            esf_text   = extract_pdf_text(save_path)
            va_from_esf = extract_va_file_number(esf_text)
            if va_from_esf:
                _update(job_id, va_file_number=va_from_esf)
        except Exception:
            pass

    _update(job_id, esf_path=save_path, esf_filename=f.filename)
    return jsonify(ok=True, esf_filename=f.filename)


# ── Phase 2: confirm + start pipeline ────────────────────────────────────

@app.route("/process/<job_id>", methods=["POST"])
def process(job_id: str):
    j = _job(job_id)
    if not j:
        return jsonify(error="Job not found"), 404
    if j.get("status") not in ("parsed", "error"):
        return jsonify(error="Job already processing or done"), 400

    body      = request.get_json(silent=True) or {}
    confirmed = body.get("confirmed_illnesses", j.get("illnesses_clean", []))

    if not confirmed:
        return jsonify(error="No conditions selected"), 400

    # Manual VA file number overrides any auto-extracted value
    manual_va = body.get("va_file_number", "")
    if manual_va:
        manual_va = re.sub(r"[^0-9]", "", str(manual_va))
    final_va = manual_va or j.get("va_file_number") or ""

    _update(job_id, status="processing", stage="downloading",
            illnesses=confirmed, logs=[], condition_packets={},
            va_file_number=final_va)

    threading.Thread(target=_pipeline, args=(job_id, confirmed), daemon=True).start()
    return jsonify(ok=True)


# ── Status polling ────────────────────────────────────────────────────────

@app.route("/status/<job_id>")
def status(job_id: str):
    j = _job(job_id)
    if not j:
        return jsonify(error="Job not found"), 404
    j.pop("text", None)
    j.pop("file_path", None)
    return jsonify(j)


# ── Per-condition packet download ─────────────────────────────────────────

@app.route("/final/<job_id>/<path:condition>")
def final_packet(job_id: str, condition: str):
    condition = unquote(condition)
    j = _job(job_id)
    if not j or j.get("status") != "done":
        return "Not ready", 400

    packets = j.get("condition_packets", {})
    path    = packets.get(condition)
    if not path or not Path(path).exists():
        return "Packet not found", 404

    vet   = re.sub(r"[^\w\s-]", "", j.get("vet_name", "")).strip().replace(" ", "_")
    cslug = re.sub(r"[^\w\s-]", "", condition).strip().replace(" ", "_")
    return send_file(
        path,
        mimetype      = "application/pdf",
        as_attachment = True,
        download_name = f"{vet}_Final_{cslug}_Packet.pdf",
    )


# ── Zip of all research PDFs ──────────────────────────────────────────────

@app.route("/download/<job_id>")
def download_zip(job_id: str):
    j = _job(job_id)
    if not j or j.get("status") != "done":
        return "Not ready", 400

    vet_name = j.get("vet_name", "output")
    folder   = Path(vet_name)
    if not folder.exists():
        return "Output folder not found", 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in folder.rglob("*"):
            if fp.is_file():
                zf.write(fp, fp.relative_to(folder.parent))
    buf.seek(0)

    safe = re.sub(r"[^\w\s-]", "", vet_name).strip().replace(" ", "_")
    return send_file(
        buf,
        mimetype      = "application/zip",
        as_attachment = True,
        download_name = f"{safe}_research_pdfs.zip",
    )


# ── Individual file serving ───────────────────────────────────────────────

@app.route("/file/<job_id>/<path:rel_path>")
def serve_file(job_id: str, rel_path: str):
    j = _job(job_id)
    if not j:
        return "Not found", 404
    full = Path(j.get("vet_name", "")) / rel_path
    if not full.exists() or not full.is_file():
        return "Not found", 404
    return send_file(str(full))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
