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
    CONDITION_PATTERNS as _COND_PATTERNS,
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


def _build_url_condition_map(
    text: str,
    urls: list[str],
    confirmed: list[str],
) -> dict[str, str]:
    """
    Assign each URL to exactly one confirmed condition.

    Strategy 1 — section-based parsing:
        Scan each line for a short condition-header line (≤80 chars, ending
        in ':', matching a CONDITION_PATTERN).  URLs that appear between two
        such headers are assigned to the first header's condition.
        Handles formats like:
            "Sleep Apnea:"
            "I. Obstructive Sleep Apnea (OSA):"
            "Re: Sleep Apnea"

    Strategy 2 — pattern proximity:
        For unassigned URLs, search for the nearest regex match from
        CONDITION_PATTERNS in the full text (no hard distance cap).
        Uses all synonyms/abbreviations, not just the canonical name.

    Strategy 3 — fallback:
        Any still-unassigned URL goes to confirmed[0].
    """
    if not urls:
        return {}
    if len(confirmed) == 1:
        return {u: confirmed[0] for u in urls}

    url_to_cond: dict[str, str] = {}

    # ── Strategy 1: section-header-based ─────────────────────────────────
    # Walk line-by-line, building (char_offset, canonical_condition) markers.
    section_markers: list[tuple[int, str]] = []
    char_pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        # Only consider short lines — section headers are never long paragraphs
        if stripped and len(stripped) <= 80:
            # Remove leading numbering / label prefixes before matching
            test = re.sub(
                r"^(?:re:|condition:|claimed\s+condition:|[ivxlIVXL]+\.?\s*|\d+\.\s*)",
                "", stripped, flags=re.IGNORECASE,
            ).strip().rstrip(":").strip()

            # Line must consist only of words/spaces/parens/hyphens (no long
            # prose sentences) — this stops paragraph lines from being headers
            if test and re.fullmatch(r"[\w\s\(\)/\-,'.]+", test):
                for pat, canonical in _COND_PATTERNS:
                    if canonical in confirmed and re.search(pat, test, re.IGNORECASE):
                        section_markers.append((char_pos, canonical))
                        break

        char_pos += len(line) + 1   # +1 for the newline

    section_markers.sort(key=lambda x: x[0])

    for url in urls:
        url_pos = text.find(url)
        if url_pos == -1:
            continue
        for i, (mpos, cond) in enumerate(section_markers):
            next_pos = (section_markers[i + 1][0]
                        if i + 1 < len(section_markers) else len(text))
            if mpos <= url_pos < next_pos:
                url_to_cond[url] = cond
                break

    # ── Strategy 2: pattern proximity ────────────────────────────────────
    # For each unassigned URL, find the nearest occurrence of ANY synonym /
    # abbreviation from CONDITION_PATTERNS (not just the canonical name).
    for url in urls:
        if url in url_to_cond:
            continue
        url_pos = text.find(url)
        if url_pos == -1:
            url_to_cond[url] = confirmed[0]
            continue

        best_cond = None
        best_dist = float("inf")
        for pat, canonical in _COND_PATTERNS:
            if canonical not in confirmed:
                continue
            for m in re.finditer(pat, text, re.IGNORECASE):
                d = abs(m.start() - url_pos)
                if d < best_dist:
                    best_dist, best_cond = d, canonical

        url_to_cond[url] = best_cond if best_cond else confirmed[0]

    # ── Strategy 3: any remaining → first condition ───────────────────────
    for url in urls:
        url_to_cond.setdefault(url, confirmed[0])

    return url_to_cond


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
        j                    = _job(job_id)
        vet_name             = j["vet_name"]
        va_file_number       = j.get("va_file_number", "")
        esf_path             = j.get("esf_path")
        conditions_with_urls = j.get("conditions_with_urls", [])

        log(f"Veteran   : {vet_name}")
        log(f"VA File#  : {va_file_number or '(not found)'}")
        log(f"Conditions: {', '.join(confirmed)}")

        # ── Stage 1: download user-provided research PDFs ─────────────────────
        _update(job_id, stage="downloading")

        job_dl_dir = OUTPUT_DIR / job_id / "research"
        job_dl_dir.mkdir(parents=True, exist_ok=True)

        cond_folders: dict[str, Path] = {}
        for cond in confirmed:
            slug_dir = job_dl_dir / _cond_slug(cond)
            slug_dir.mkdir(exist_ok=True)
            cond_folders[cond] = slug_dir

        # Build URL → condition maps from user-supplied URLs only
        url_to_cond:  dict[str, str]       = {}
        url_to_conds: dict[str, set[str]]  = {}

        cond_url_map: dict[str, list[str]] = {
            entry["condition"]: entry.get("urls", [])
            for entry in conditions_with_urls
        }

        for cond in confirmed:
            user_urls = cond_url_map.get(cond, [])
            log(f"\n[urls] {cond}: {len(user_urls)} URL(s)")
            for raw in user_urls:
                u = raw.strip()
                if not u:
                    continue
                if u not in url_to_cond:
                    url_to_cond[u] = cond
                url_to_conds.setdefault(u, set()).add(cond)
                log(f"  {u}")

        # Build url_map — one entry per unique URL, may target multiple folders
        all_urls_ordered: list[str] = list(url_to_conds.keys())
        url_map: list[tuple[str, list[str]]] = []
        for url in all_urls_ordered:
            target_folders = [
                str(cond_folders[c])
                for c in url_to_conds[url]
                if c in cond_folders
            ]
            url_map.append((url, target_folders))

        _update(job_id, total_search_urls=len(all_urls_ordered))
        log(f"\nDownloading {len(url_map)} article(s)…")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            dl_results = loop.run_until_complete(downloader.run_downloads(url_map))
        finally:
            loop.close()

        log(f"Downloaded: {dl_results['downloaded']}  |  Skipped: {dl_results['skipped']}")

        # ── Build per-condition successful_pdfs from saved_paths ──────────────
        # Determine which condition each saved file belongs to by its folder path.
        successful_pdfs: dict[str, list[str]] = {cond: [] for cond in confirmed}

        def _path_to_cond(p: str) -> str | None:
            pp = Path(p).resolve()
            for c, folder in cond_folders.items():
                try:
                    pp.relative_to(folder.resolve())
                    return c
                except ValueError:
                    continue
            return None

        for entry in dl_results["results"]:
            if entry.get("downloaded") and entry.get("saved_paths"):
                for p in entry["saved_paths"]:
                    if Path(p).exists():
                        cond_for_path = _path_to_cond(p)
                        if cond_for_path:
                            successful_pdfs[cond_for_path].append(p)
                            print(f"[render-success] {p}", flush=True)
                        else:
                            print(f"[render-success] unmatched path: {p}", flush=True)
                    else:
                        print(f"[render-fail] downloaded=True but file missing: {p}",
                              flush=True)
            else:
                print(f"[render-fail] url={entry['url']!r} "
                      f"downloaded={entry.get('downloaded')}",
                      flush=True)

        # Keep illness_files for UI/status reporting only (not used for merge)
        illness_files: dict[str, list[str]] = {
            cond: successful_pdfs[cond] for cond in confirmed
        }

        article_results: list[dict] = []
        for entry in dl_results["results"]:
            m    = re.search(r"/(\d+)/?$", entry["url"])
            pmid = m.group(1) if m else ""
            article_results.append({
                "url":        entry["url"],
                "downloaded": entry["downloaded"],
                "pmid":       pmid,
                "filename":   f"PMID_{pmid}.pdf" if pmid else "article.pdf",
                # enriched fields for retry/upload workflow
                "status":      "ok" if entry.get("downloaded") else "failed",
                "conditions":  list(url_to_conds.get(entry["url"], set())),
                "saved_paths": entry.get("saved_paths", []),
                "error":       "" if entry.get("downloaded") else "Render failed",
            })

        # ── Stage 2 & 3: per-condition ESF fill + merge + headers ─────
        condition_packets:  dict[str, str] = {}
        filled_esf_paths:   dict[str, str] = {}   # kept so rebuild skips re-fill

        if esf_path and Path(esf_path).exists():
            for cond in confirmed:
                slug        = _cond_slug(cond)
                cond_pdfs   = successful_pdfs.get(cond, [])

                print(f"[merge] condition={cond!r}", flush=True)
                print(f"[merge] successful_pdfs={cond_pdfs}", flush=True)

                cond_entries = [
                    a for a in article_results
                    if a["downloaded"] and cond in url_to_conds.get(a["url"], set())
                ]

                # Fill ESF
                _update(job_id, stage="filling_esf", current_condition=cond)
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
                filled_esf_paths[cond] = filled_esf

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
            filled_esf_paths   = filled_esf_paths,
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


# ── Phase 0: create blank job (no memo) ──────────────────────────────────

@app.route("/new-job", methods=["POST"])
def new_job():
    """Create an empty job when no memorandum is uploaded."""
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        jobs[job_id] = {
            "id":               job_id,
            "filename":         None,
            "file_path":        None,
            "esf_path":         None,
            "esf_filename":     None,
            "text":             "",
            "status":           "parsed",
            "stage":            "confirm",
            "vet_name":         "",
            "va_file_number":   "",
            "illnesses_clean":  [],
            "illnesses":        [],
            "extraction_debug": ["No memo uploaded — conditions entered manually."],
            "urls":             [],
            "logs":             [],
            "article_results":  [],
            "illness_files":    {},
            "dl_summary":       {},
            "condition_packets":{},
            "current_condition": "",
            "error":            None,
            "created":          time.time(),
        }
    return jsonify(
        job_id          = job_id,
        vet_name        = "",
        va_file_number  = "",
        illnesses_clean = [],
        extraction_debug= ["No memo uploaded — conditions entered manually."],
        url_count       = 0,
    )


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

    body = request.get_json(silent=True) or {}

    # New primary format: [{condition, urls}, …]
    conditions_with_urls: list[dict] = body.get("conditions_with_urls", [])
    if conditions_with_urls:
        confirmed = [
            c["condition"] for c in conditions_with_urls if c.get("condition", "").strip()
        ]
    else:
        # Fallback for legacy callers
        confirmed = body.get("confirmed_illnesses", j.get("illnesses_clean", []))

    if not confirmed:
        return jsonify(error="No conditions provided"), 400

    # Manual VA file number overrides any auto-extracted value
    manual_va = body.get("va_file_number", "")
    if manual_va:
        manual_va = re.sub(r"[^0-9]", "", str(manual_va))
    final_va = manual_va or j.get("va_file_number") or ""

    _update(job_id,
            status              = "processing",
            stage               = "downloading",
            illnesses           = confirmed,
            conditions_with_urls= conditions_with_urls,
            logs                = [],
            condition_packets   = {},
            va_file_number      = final_va)

    threading.Thread(target=_pipeline, args=(job_id, confirmed), daemon=True).start()
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────────────
# Partial-rebuild helpers  (retry / manual upload)
# ─────────────────────────────────────────────────────────────────────────

def _recalculate_dl_summary(job_id: str) -> None:
    """
    Recount dl_summary.downloaded / skipped from the live article_results list.
    Called after any retry or manual-upload mutation so stat cards stay accurate.
    """
    with _lock:
        if job_id not in jobs:
            return
        results    = jobs[job_id].get("article_results", [])
        downloaded = sum(1 for a in results if a.get("downloaded"))
        skipped    = len(results) - downloaded
        ds = jobs[job_id].setdefault("dl_summary", {})
        ds["downloaded"] = downloaded
        ds["skipped"]    = skipped


def _rebuild_packet(job_id: str, cond: str) -> str:
    """
    Re-merge ONE condition packet from the already-signed ESF + current
    illness_files list.  Does NOT re-run ESF fill or signature stamping —
    those are expensive and correct.  Only the merge step is repeated.
    Returns the path of the rebuilt packet.
    """
    j           = _job(job_id)
    slug        = _cond_slug(cond)
    vet_name    = j.get("vet_name", "")
    va_file_num = j.get("va_file_number", "")

    filled_esf = j.get("filled_esf_paths", {}).get(cond)
    if not filled_esf or not Path(filled_esf).exists():
        raise FileNotFoundError(
            f"Signed ESF not found for condition {cond!r} — "
            "cannot rebuild without re-running the full pipeline."
        )

    cond_pdfs   = j.get("illness_files", {}).get(cond, [])
    packet_path = str(OUTPUT_DIR / f"{job_id}_Final_{slug}_Packet.pdf")

    print(f"[rebuild] {cond!r}  esf={filled_esf}  pdfs={len(cond_pdfs)}",
          flush=True)

    esf_filler.build_condition_packet(
        filled_esf_path    = filled_esf,
        research_pdf_paths = cond_pdfs,
        output_path        = packet_path,
        vet_name           = vet_name,
        va_file_number     = va_file_num,
        condition          = cond,
    )

    with _lock:
        if job_id in jobs:
            jobs[job_id].setdefault("condition_packets", {})[cond] = packet_path

    print(f"[rebuild] done → {Path(packet_path).name}", flush=True)
    return packet_path


# ── Retry a single failed article ─────────────────────────────────────────

@app.route("/retry-article/<job_id>", methods=["POST"])
def retry_article(job_id: str):
    j = _job(job_id)
    if not j:
        return jsonify(error="Job not found"), 404

    data = request.get_json(silent=True) or {}
    cond = data.get("condition", "").strip()
    url  = data.get("url", "").strip()
    if not cond or not url:
        return jsonify(error="condition and url required"), 400

    cond_folder = OUTPUT_DIR / job_id / "research" / _cond_slug(cond)
    cond_folder.mkdir(parents=True, exist_ok=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        dl = loop.run_until_complete(
            downloader.run_downloads([(url, [str(cond_folder)])])
        )
    finally:
        loop.close()

    entry = dl["results"][0] if dl.get("results") else {}
    ok    = entry.get("downloaded", False)

    if ok:
        # Merge new PDFs into illness_files
        with _lock:
            if job_id in jobs:
                ifiles = jobs[job_id].setdefault("illness_files", {})
                ifiles.setdefault(cond, [])
                for p in entry.get("saved_paths", []):
                    if Path(p).exists() and p not in ifiles[cond]:
                        ifiles[cond].append(p)
                # Update article status
                for a in jobs[job_id].get("article_results", []):
                    if a["url"] == url:
                        a["status"]      = "ok"
                        a["downloaded"]  = True
                        a["saved_paths"] = entry.get("saved_paths", [])
                        a["error"]       = ""

        _recalculate_dl_summary(job_id)

        pdf_count = len(_job(job_id).get("illness_files", {}).get(cond, []))
        has_esf   = bool(_job(job_id).get("filled_esf_paths", {}).get(cond))

        if has_esf:
            try:
                _rebuild_packet(job_id, cond)
                return jsonify(ok=True, status="ok",
                               packet_url=f"/final/{job_id}/{cond}",
                               pdf_count=pdf_count)
            except Exception as exc:
                return jsonify(ok=False, error=str(exc)), 500
        else:
            return jsonify(ok=True, status="ok",
                           packet_url=None, pdf_count=pdf_count,
                           has_esf=False)
    else:
        return jsonify(ok=False, status="failed",
                       error="Render failed — PMC may be blocking this IP")


# ── Accept a manually-uploaded replacement PDF ────────────────────────────

@app.route("/upload-article-pdf/<job_id>", methods=["POST"])
def upload_article_pdf(job_id: str):
    j = _job(job_id)
    if not j:
        return jsonify(error="Job not found"), 404

    cond = request.form.get("condition", "").strip()
    url  = request.form.get("url", "").strip()
    f    = request.files.get("file")
    if not cond or not f:
        return jsonify(error="condition and file are required"), 400

    # Save uploaded PDF to the condition research folder
    cond_folder = OUTPUT_DIR / job_id / "research" / _cond_slug(cond)
    cond_folder.mkdir(parents=True, exist_ok=True)

    existing   = list(cond_folder.glob("*.pdf"))
    next_idx   = len(existing) + 1
    safe_name  = re.sub(r"[^\w.-]", "_", f.filename or "manual.pdf")
    save_path  = str(cond_folder / f"{next_idx:02d}_manual_{safe_name}")
    f.save(save_path)

    # Update job state
    with _lock:
        if job_id in jobs:
            ifiles = jobs[job_id].setdefault("illness_files", {})
            ifiles.setdefault(cond, [])
            if save_path not in ifiles[cond]:
                ifiles[cond].append(save_path)
            # Mark article row as manually uploaded
            if url:
                for a in jobs[job_id].get("article_results", []):
                    if a["url"] == url:
                        a["status"]     = "manual"
                        a["downloaded"] = True
                        a["error"]      = ""

    _recalculate_dl_summary(job_id)

    pdf_count = len(_job(job_id).get("illness_files", {}).get(cond, []))
    has_esf   = bool(_job(job_id).get("filled_esf_paths", {}).get(cond))

    if has_esf:
        try:
            _rebuild_packet(job_id, cond)
            return jsonify(ok=True, packet_url=f"/final/{job_id}/{cond}",
                           pdf_count=pdf_count, has_esf=True)
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 500
    else:
        # No ESF provided — PDF is saved to folder but no merged packet to rebuild
        return jsonify(ok=True, packet_url=None, pdf_count=pdf_count, has_esf=False)


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
