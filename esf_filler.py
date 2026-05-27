"""
ESF PDF Filler — Per-Condition Evidence Packet Builder

For each claimed condition this module:
  1. Fills a copy of the ESF:
       • Places an X mark at the "OTHER (Describe)" checkbox
       • Writes "<Condition> Research Document Attached" in the describe area
  2. Merges: filled ESF  +  condition's research PDFs
  3. Stamps a header (<Vet Name> / <VA File Number>) on every page
     starting at page 3 of the merged document

Strategy for flat/scanned ESFs:
  • pdfplumber to locate text anchors on each page
  • reportlab to render a transparent overlay at those coordinates
  • pypdf to stamp the overlay onto the original PDF pages

For fillable AcroForm PDFs the AcroForm fields are filled first.
"""

import io
import re
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import black
from reportlab.pdfgen import canvas as rl_canvas


def _prepare_sig_image(sig_path: Path) -> io.BytesIO:
    """
    Return a BytesIO PNG with proper RGBA transparency so reportlab can
    composite the signature cleanly with mask='auto'.

    • If the source PNG already has an alpha channel → kept as-is.
    • If it has a solid white/near-white background → convert to RGBA and
      set near-white pixels (R>240, G>240, B>240) to fully transparent.

    Uses Pillow (installed as a transitive dependency of pdfplumber).
    Falls back to a raw file-copy if Pillow is unavailable.
    """
    try:
        from PIL import Image as _PILImage
        img = _PILImage.open(str(sig_path))
        if img.mode == "RGBA":
            # Already has a real alpha channel — use directly
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf
        img = img.convert("RGBA")
        pixels = list(img.getdata())
        new_pixels = [
            (r, g, b, 0) if (r > 240 and g > 240 and b > 240) else (r, g, b, a)
            for (r, g, b, a) in pixels
        ]
        img.putdata(new_pixels)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        # Pillow unavailable or image unreadable — return raw bytes
        buf = io.BytesIO(sig_path.read_bytes())
        buf.seek(0)
        return buf

# ── Typography constants ──────────────────────────────────────────────────
_F          = "Helvetica"
_FB         = "Helvetica-Bold"
_FSML       = 8
_FREG       = 9
_FHDR       = 8
_LH         = 13
_MARK_FONT  = 8        # point size for the "X" mark glyph (must fit inside checkbox)
_CHECKBOX_OFFSET = 9   # points left of "OTHER" text where the checkbox sits


# ─────────────────────────────────────────────────────────────────────────
# Coordinate helpers
# ─────────────────────────────────────────────────────────────────────────

def _pw_ph(page) -> tuple[float, float]:
    return float(page.width), float(page.height)


def _rl_y(pdfplumber_top: float, page_height: float) -> float:
    """pdfplumber 'top' (from page top) → reportlab y (from page bottom)."""
    return page_height - pdfplumber_top


def _words(page) -> list[dict]:
    try:
        return page.extract_words(x_tolerance=3, y_tolerance=3)
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────
# "OTHER (Describe)" locator
# ─────────────────────────────────────────────────────────────────────────

def _find_other_describe(words: list[dict], ph: float) -> dict | None:
    """
    Locate the 'OTHER (Describe)' area on a page.

    Searches for a word containing 'OTHER' and a nearby word containing
    'Describe' (within ±8 pts vertically or immediately after).  Handles
    common VA-form layouts where the two tokens may be:
      • separate words on the same line:  "OTHER  (Describe):"
      • merged into one token:            "OTHER(Describe)"
      • split across tokens with punctuation

    Returns a dict with reportlab coordinates or None if not found.
    """
    other_w    = None
    describe_w = None

    for w in words:
        t = w.get("text", "")
        # Match the word that starts with OTHER (may have trailing punctuation)
        if re.search(r"\bOTHER\b", t, re.IGNORECASE) and other_w is None:
            other_w = w
            # If this single token already contains "Describe" (merged token),
            # treat it as both anchor and describe label.
            if re.search(r"Describe", t, re.IGNORECASE):
                describe_w = w
                break
        elif other_w is not None and describe_w is None:
            # Accept any subsequent word containing "Describe" that is on the
            # same horizontal band (within 8 pts) or up to 2 words later.
            vert_diff = abs(float(w.get("top", 0)) - float(other_w.get("top", 0)))
            if re.search(r"Describe", t, re.IGNORECASE) and vert_diff <= 8:
                describe_w = w
                break

    if other_w is None:
        return None

    ox0   = float(other_w["x0"])
    o_top = float(other_w["top"])
    o_bot = float(other_w["bottom"])
    o_mid = (o_top + o_bot) / 2

    # Checkbox sits _CHECKBOX_OFFSET points to the left of the "OTHER" text
    x_mark = ox0 - _CHECKBOX_OFFSET
    y_mark = _rl_y(o_mid, ph)

    if describe_w and describe_w is not other_w:
        # Place the condition text right after the "(Describe):" label
        x_text = float(describe_w["x1"]) + 5
        y_text = _rl_y(float(describe_w["bottom"]) - 1, ph)
    elif describe_w is other_w:
        # Merged token — place text after the whole token
        x_text = float(other_w["x1"]) + 5
        y_text = _rl_y(o_bot, ph)
    else:
        # No "(Describe)" found — put text to the right of "OTHER"
        x_text = float(other_w["x1"]) + 30
        y_text = _rl_y(o_bot, ph)

    return {
        "x_mark": x_mark, "y_mark": y_mark,
        "x_text": x_text, "y_text": y_text,
        "page_width": float(words[0].get("x1", 612)) if words else 612,
    }


# ─────────────────────────────────────────────────────────────────────────
# Overlay builders
# ─────────────────────────────────────────────────────────────────────────

def _build_condition_overlay(
    page_sizes:   list[tuple[float, float]],
    anchor_map:   dict[int, dict],
    condition:    str,
    research_entries: list[dict],
) -> bytes:
    """
    Build a transparent overlay PDF with:
      • Bold X mark at the checkbox
      • "<Condition> Research Document Attached" in the describe area
      • Small research file list below (if room)
    """
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf)

    for page_idx, (pw, ph) in enumerate(page_sizes):
        c.setPageSize((pw, ph))
        c.setFillColor(black)

        anchors = anchor_map.get(page_idx)
        if anchors:
            xm, ym = anchors["x_mark"], anchors["y_mark"]
            xt, yt = anchors["x_text"], anchors["y_text"]

            # ── X mark ─────────────────────────────────────────────
            # Center the glyph inside the checkbox square.
            # drawString places the bottom-left of the character at (x, y).
            # Cap-height of Helvetica-Bold ≈ 72% of font size.
            # Horizontal: shift +2 pt so the X sits in the middle of the ~10 pt box.
            c.setFont(_FB, _MARK_FONT)
            cap_h  = _MARK_FONT * 0.72
            c.drawString(xm + 2, ym - cap_h / 2, "X")

            # ── Condition text ──────────────────────────────────────
            label = f"{condition} Research Document Attached"
            c.setFont(_FB, _FREG)
            # Check if there's enough horizontal room on the same line
            remaining_w = pw - xt - 20
            label_w     = c.stringWidth(label, _FB, _FREG)
            if label_w <= remaining_w:
                c.drawString(xt, yt, label)
            else:
                # Drop to the next line below the OTHER row
                c.drawString(anchors.get("x_mark", xt), yt - _LH, label)

            # ── Small research list ─────────────────────────────────
            if research_entries:
                c.setFont(_F, _FSML)
                y = yt - _LH * 2
                for i, e in enumerate(research_entries, 1):
                    pmid  = e.get("pmid", "")
                    fname = e.get("filename", "")
                    item  = f"{i}. {'PMID ' + pmid if pmid else fname}"
                    if y < 36:
                        break
                    c.drawString(xt, y, item)
                    y -= (_FSML + 3)

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


def _build_header_overlay(
    page_sizes:     list[tuple[float, float]],
    start_idx:      int,
    vet_name:       str,
    va_file_number: str,
) -> bytes:
    """
    Build a transparent overlay that stamps a two-line header
    (vet_name / va_file_number) at the TOP LEFT of every page
    whose index is >= start_idx.  Format matches legal-document style.
    """
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf)

    for page_idx, (pw, ph) in enumerate(page_sizes):
        c.setPageSize((pw, ph))

        if page_idx >= start_idx:
            c.setFillColor(black)
            c.setFont(_FB, _FHDR)
            left = 36          # 0.5 inch left margin
            c.drawString(left, ph - 14,              vet_name)
            c.drawString(left, ph - 14 - (_FHDR + 2), va_file_number)

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────────────
# AcroForm filler (fillable PDFs) — handles text fields AND checkboxes
# ─────────────────────────────────────────────────────────────────────────

def _acroform_on_value(field_obj) -> str:
    """
    Return the 'On' state name for a checkbox / radio-button field.
    Most VA forms use '/Yes', '/On', or a custom state name found
    in the normal appearance dict (/AP /N).
    """
    try:
        ap = field_obj.get("/AP", {})
        n  = ap.get("/N", {})
        for k in n:
            if k not in ("/Off", "/off") and k.startswith("/"):
                return k
    except Exception:
        pass
    return "/Yes"


def _try_acroform(
    esf_path:         str,
    output_path:      str,
    condition:        str,
    research_entries: list[dict],
) -> bool:
    """
    Fill an AcroForm PDF:
      • Check the 'OTHER' / 'Other' checkbox (field type /Btn)
      • Fill the Describe / Other-text field with
        '<condition> Research Document Attached'

    Returns True if at least one field was written.
    """
    reader = PdfReader(esf_path)
    all_fields = reader.get_fields()
    if not all_fields:
        print("  [esf-acroform] No AcroForm fields found")
        return False

    label = f"{condition} Research Document Attached"
    research_lines = "\n".join(
        f"PMID {e['pmid']}" if e.get("pmid") else e.get("filename", "")
        for e in research_entries
    ) or label

    updates: dict[str, str] = {}

    print(f"  [esf-acroform] Found {len(all_fields)} field(s): {list(all_fields)[:10]}")

    for name, field_obj in all_fields.items():
        nl  = name.lower()
        ft  = field_obj.get("/FT", "")

        # ── Checkbox / radio button for "OTHER" ──────────────────
        if ft == "/Btn":
            if any(k in nl for k in ("other", "oth", "claim_type", "claimtype",
                                     "purpose", "type")):
                on_val = _acroform_on_value(field_obj)
                updates[name] = on_val
                print(f"  [esf-acroform] Checking '{name}' → {on_val}")

        # ── Text field for the description ───────────────────────
        elif ft == "/Tx":
            if any(k in nl for k in ("describe", "description", "other_text",
                                     "othertext", "condition", "diagnosis",
                                     "disabilit", "claim", "purpose_desc",
                                     "explain", "specify")):
                updates[name] = label
            elif any(k in nl for k in ("research", "attach", "evidence",
                                       "article", "document", "list")):
                updates[name] = research_lines

    if not updates:
        print("  [esf-acroform] No matching fields — falling back to overlay")
        return False

    writer = PdfWriter()
    writer.append(reader)
    # Apply updates to every page (some multi-page forms have fields on p2)
    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, updates)
        except Exception:
            pass

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as fh:
        writer.write(fh)
    print(f"  [esf-acroform] Wrote {len(updates)} field update(s) → {output_path}")
    return True


# ─────────────────────────────────────────────────────────────────────────
# Overlay filler (flat / scanned PDFs)
# ─────────────────────────────────────────────────────────────────────────

def _fill_overlay(
    esf_path:         str,
    output_path:      str,
    condition:        str,
    research_entries: list[dict],
) -> None:
    page_sizes: list[tuple[float, float]] = []
    anchor_map: dict[int, dict]           = {}

    with pdfplumber.open(esf_path) as pdf:
        for idx, page in enumerate(pdf.pages):
            pw, ph = _pw_ph(page)
            page_sizes.append((pw, ph))
            ws      = _words(page)
            anchors = _find_other_describe(ws, ph)
            if anchors:
                anchor_map[idx] = anchors
                print(
                    f"  [esf-overlay] 'OTHER(Describe)' found p{idx+1} "
                    f"x_mark={anchors['x_mark']:.0f} x_text={anchors['x_text']:.0f}"
                )

    if not anchor_map:
        print("  [esf-overlay] 'OTHER (Describe)' not found — using summary page fallback")
        _append_summary_page(esf_path, output_path, condition, research_entries)
        return

    overlay_bytes  = _build_condition_overlay(page_sizes, anchor_map, condition, research_entries)
    orig_reader    = PdfReader(esf_path)
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    writer         = PdfWriter()

    for i, orig_page in enumerate(orig_reader.pages):
        if i < len(overlay_reader.pages):
            # merge_page(other) draws `other` ON TOP of self.
            # Start with orig, merge overlay on top → X mark / text is visible.
            orig_page.merge_page(overlay_reader.pages[i])
            writer.add_page(orig_page)
        else:
            writer.add_page(orig_page)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as fh:
        writer.write(fh)
    print(f"  [esf-overlay] Written → {output_path}")


def _append_summary_page(
    esf_path:         str,
    output_path:      str,
    condition:        str,
    research_entries: list[dict],
) -> None:
    """Last-resort: append a condition summary page to an unmodified ESF copy."""
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=letter)
    pw, ph = letter

    label = f"{condition} Research Document Attached"
    c.setFont(_FB, 13)
    c.drawString(54, ph - 72, label)

    c.setFont(_F, 9)
    y = ph - 96
    for i, e in enumerate(research_entries, 1):
        pmid  = e.get("pmid", "")
        fname = e.get("filename", "")
        item  = f"{i}. {'PMID ' + pmid if pmid else fname}"
        c.drawString(54, y, item)
        y -= 13
        if y < 72:
            break

    c.showPage()
    c.save()
    buf.seek(0)

    writer = PdfWriter()
    writer.append(PdfReader(esf_path))
    writer.append(PdfReader(buf))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as fh:
        writer.write(fh)
    print(f"  [esf-summary] Fallback page appended → {output_path}")


# ─────────────────────────────────────────────────────────────────────────
# Public: fill ESF for one condition
# ─────────────────────────────────────────────────────────────────────────

def fill_esf_for_condition(
    esf_path:         str,
    output_path:      str,
    condition:        str,
    research_entries: list[dict],
) -> str:
    """
    Fill a copy of the ESF for a single claimed condition.

    Locates the 'OTHER (Describe)' checkbox, stamps an X, and writes
    '<condition> Research Document Attached' in the describe area.

    Both the AcroForm path (field values) AND the visual overlay path
    (explicit X mark drawn via reportlab) are always applied.
    pypdf's AcroForm update does not regenerate appearance streams, so the
    checkbox would be invisible without the visual overlay running too.

    Parameters
    ----------
    esf_path         : original ESF PDF
    output_path      : where to write the filled copy
    condition        : e.g. 'Sleep Apnea'
    research_entries : list of dicts with 'pmid', 'filename', 'downloaded'
    """
    print(f"\n[fill-esf] condition={condition!r}")

    # Step 1 — AcroForm: update field values (text fields, checkbox states).
    # Even when this succeeds, we still run the visual overlay below because
    # pypdf doesn't rebuild /AP appearance streams, leaving checkboxes invisible.
    acroform_ok = _try_acroform(esf_path, output_path, condition, research_entries)

    # Step 2 — Visual overlay: always draw the explicit X mark + condition text.
    # Source is the acroform-updated file (if it was written), otherwise original.
    overlay_source = (output_path
                      if acroform_ok and Path(output_path).exists()
                      else esf_path)
    _fill_overlay(overlay_source, output_path, condition, research_entries)
    return output_path


# ─────────────────────────────────────────────────────────────────────────
# Public: add headers to pages 3+ of an existing PDF
# ─────────────────────────────────────────────────────────────────────────

def add_page_headers(
    input_pdf:      str,
    output_pdf:     str,
    vet_name:       str,
    va_file_number: str,
    start_page_idx: int = 2,        # 0-indexed; default = page 3
) -> str:
    """
    Stamp a right-aligned header block (vet_name / va_file_number)
    onto every page whose 0-based index is >= start_page_idx.

    Parameters
    ----------
    input_pdf      : merged PDF to annotate
    output_pdf     : where to write the annotated copy
    vet_name       : e.g. 'Taylor Scott'
    va_file_number : e.g. '023568588'
    start_page_idx : first page (0-indexed) that gets a header (default 2 = page 3)
    """
    if not (vet_name or va_file_number):
        # Nothing to add — just copy
        import shutil
        shutil.copy2(input_pdf, output_pdf)
        return output_pdf

    reader     = PdfReader(input_pdf)
    page_sizes : list[tuple[float, float]] = []

    with pdfplumber.open(input_pdf) as pdf:
        for page in pdf.pages:
            page_sizes.append(_pw_ph(page))

    overlay_bytes  = _build_header_overlay(page_sizes, start_page_idx, vet_name or "", va_file_number or "")
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    writer         = PdfWriter()

    for i, orig_page in enumerate(reader.pages):
        if i < len(overlay_reader.pages):
            orig_page.merge_page(overlay_reader.pages[i])
        writer.add_page(orig_page)

    Path(output_pdf).parent.mkdir(parents=True, exist_ok=True)
    with open(output_pdf, "wb") as fh:
        writer.write(fh)

    pages_with_header = max(0, len(reader.pages) - start_page_idx)
    print(f"  [headers] Added to {pages_with_header} page(s) (pages {start_page_idx+1}+) → {output_pdf}")
    return output_pdf


# ─────────────────────────────────────────────────────────────────────────
# Public: build one condition's complete evidence packet
# ─────────────────────────────────────────────────────────────────────────

def _no_research_page(condition: str) -> bytes:
    """
    Build a single-page PDF that states no research links were available for
    this condition.  Used when a condition has zero associated PubMed URLs.
    """
    from reportlab.lib.pagesizes import letter
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=letter)
    pw, ph = letter

    c.setFillColor(black)
    c.setFont(_FB, 12)
    c.drawCentredString(pw / 2, ph / 2 + 20,
                        f"{condition} — No Research Available")
    c.setFont(_F, 10)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawCentredString(pw / 2, ph / 2,
                        "No medical research links were found in the memorandum for this condition.")
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def build_condition_packet(
    filled_esf_path:    str,
    research_pdf_paths: list[str],
    output_path:        str,
    vet_name:           str = "",
    va_file_number:     str = "",
    condition:          str = "",
) -> str:
    """
    Merge filled ESF + condition research PDFs, then stamp headers on pages 3+.

    If research_pdf_paths is empty a "No medical research links available"
    notice page is inserted instead so the packet is never padded with
    unrelated PDFs from other conditions.

    Parameters
    ----------
    filled_esf_path    : condition-specific filled ESF
    research_pdf_paths : PDFs that belong to THIS condition only
    output_path        : Final_<Condition>_Packet.pdf destination
    vet_name           : for page headers
    va_file_number     : for page headers
    condition          : used in the no-research notice page label
    """
    print(f"\n[merge] {Path(output_path).name}")

    # ── 1. Merge ESF + research (or no-research notice) ──────────────
    tmp_path = output_path + ".tmp.pdf"
    writer   = PdfWriter()

    esf_reader = PdfReader(filled_esf_path)
    for page in esf_reader.pages:
        writer.add_page(page)
    print(f"  [merge] ESF: {len(esf_reader.pages)} page(s)")

    valid_pdfs = sorted(p for p in research_pdf_paths if Path(p).exists())

    if valid_pdfs:
        for pdf_path in valid_pdfs:
            try:
                r = PdfReader(pdf_path)
                for page in r.pages:
                    writer.add_page(page)
                print(f"  [merge] + {Path(pdf_path).name} ({len(r.pages)} page(s))")
            except Exception as e:
                print(f"  [merge-warn] Skipping {pdf_path}: {e}")
    else:
        # Insert a visible "no research" notice instead of attaching nothing
        # or — worse — attaching another condition's PDFs.
        label = condition or Path(filled_esf_path).stem
        notice_bytes = _no_research_page(label)
        notice_reader = PdfReader(io.BytesIO(notice_bytes))
        for page in notice_reader.pages:
            writer.add_page(page)
        print(f"  [merge] No research PDFs — inserted 'no research' notice page")

    Path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, "wb") as fh:
        writer.write(fh)

    # ── 2. Stamp headers on pages 3+ ─────────────────────────────────
    add_page_headers(
        input_pdf      = tmp_path,
        output_pdf     = output_path,
        vet_name       = vet_name,
        va_file_number = va_file_number,
        start_page_idx = 2,     # 0-based → page 3 onwards
    )

    try:
        Path(tmp_path).unlink()
    except Exception:
        pass

    total = len(PdfReader(output_path).pages)
    print(f"  [merge] Total pages: {total} → {output_path}")
    return output_path


# ─────────────────────────────────────────────────────────────────────────
# Signature + date overlay  (Section VI / fields 19A & 19B)
# ─────────────────────────────────────────────────────────────────────────

from datetime import date as _date

SIGNATURE_DIR  = Path("static") / "signatures"
SIGNATURE_PATH = SIGNATURE_DIR / "jeff_signature.png"


def _today_str() -> str:
    return _date.today().strftime("%m-%d-%Y")


def _find_sig_anchors(page, ph: float) -> dict | None:
    """
    Locate Section VI / 19A / 19B area — POA signature block ONLY.

    Strategy:
      1. Find "SECTION VI" (or "VI.") header to anchor the search region.
         If not found, restrict to the bottom 38% of the page.
      2. Search ONLY below that anchor for 19A (signature) and 19B (date).
         Tight keywords prevent false matches in Sections IV/V.
    """
    words = _words(page)
    full_text = " ".join(w.get("text", "") for w in words).upper()

    # ── Step 1: find the vertical start of Section VI ─────────────────
    sec6_top: float | None = None
    for w in words:
        t = w.get("text", "").upper().strip(".,: ")
        # Match "SECTION VI", "VI.", "SECTION 6", or a standalone "VI"
        if ("SECTION" in t and ("VI" in t or "6" in t)):
            sec6_top = float(w.get("top", 0))
            break
        if re.match(r"^VI\.?$", t):
            sec6_top = float(w.get("top", 0))
            break

    # If no Section VI header found, search bottom 38% of page only
    search_from = sec6_top if sec6_top is not None else ph * 0.62

    # ── Step 2: find 19A (signature) and 19B (date) in that region ────
    #   Very tight keywords — must NOT match Section IV/V date fields.
    SIG_KW  = ("19a", "poa/auth", "poa auth", "authorized rep",
                "power of attorney", "attorney sig")
    DATE_KW = ("19b", "date signed", "mm-dd-yyyy", "mm/dd/yyyy")

    sig_w  = None
    date_w = None

    for w in words:
        if float(w.get("top", 0)) < search_from:
            continue
        t = w.get("text", "").lower()
        if sig_w is None and any(k in t for k in SIG_KW):
            sig_w = w
        if date_w is None and any(k in t for k in DATE_KW):
            date_w = w

    # If tight match failed but we know we're in Section VI region,
    # accept a broader "signature" keyword within the section band.
    if sig_w is None and sec6_top is not None:
        for w in words:
            if float(w.get("top", 0)) < search_from:
                continue
            t = w.get("text", "").lower()
            if sig_w is None and any(k in t for k in ("signature", "representative", "authorized")):
                sig_w = w
            if date_w is None and "date" in t:
                date_w = w

    if sig_w is None:
        return None

    sig_top  = float(sig_w["top"])
    sig_left = float(sig_w.get("x0", 72))
    sig_right = float(sig_w.get("x1", sig_left + 60))
    # Place the signature image to the RIGHT of the label text, inside the
    # blank field box.  Use a comfortable gap (12 pt) after the label's right
    # edge, then shift down 8 pt from the label's top so it sits in the field.
    sig_x    = sig_right + 12
    sig_y    = _rl_y(sig_top + 8, ph)

    if date_w:
        date_top  = float(date_w["top"])
        date_left = float(date_w.get("x0", 350))
        # Write the date AT the field position (overlays the MM-DD-YYYY hint).
        # Baseline is centred in the label's bounding box height.
        date_x    = date_left
        date_y    = _rl_y(date_top + float(date_w.get("height", 10)) * 0.5 + 4, ph)
    else:
        date_y = sig_y
        date_x = sig_x + 200

    return {"sig_x": sig_x, "sig_y": sig_y, "date_x": date_x, "date_y": date_y}


def _build_sig_overlay(
    page_sizes:  list[tuple[float, float]],
    page_idx:    int,
    anchors:     dict,
    sig_path:    Path,
    date_str:    str,
) -> bytes:
    """Build a transparent overlay PDF with the signature image + date string."""
    from reportlab.lib.utils import ImageReader

    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf)

    for i, (pw, ph) in enumerate(page_sizes):
        c.setPageSize((pw, ph))
        if i == page_idx:
            # ── Signature image ──────────────────────────────────
            # _prepare_sig_image() normalises the PNG to RGBA, converting any
            # solid-white background to transparent pixels. mask='auto' then
            # reads the alpha channel directly — no black rectangle artifact.
            try:
                sig_buf = _prepare_sig_image(sig_path)
                c.drawImage(
                    ImageReader(sig_buf),
                    anchors["sig_x"],
                    anchors["sig_y"],
                    width  = 130,
                    height = 32,
                    preserveAspectRatio = True,
                    mask = "auto",
                )
            except Exception as exc:
                print(f"  [sig-warn] Cannot draw signature image: {exc}")

            # ── Date ─────────────────────────────────────────────
            c.setFillColor(black)
            c.setFont(_F, 10)
            c.drawString(anchors["date_x"], anchors["date_y"], date_str)

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


def apply_signature_and_date(
    esf_path:    str,
    output_path: str,
    sig_path:    "Path | None" = None,
) -> str:
    """
    Stamp the Jeff signature image + today's date onto the ESF.

    • Tries AcroForm date field first (fills 19B text widget).
    • Then overlays the signature image via pdfplumber text anchors.
    • If no signature file is saved, the file is left unchanged.

    Parameters
    ----------
    esf_path    : source filled-ESF PDF
    output_path : destination (can be the same path as esf_path)
    sig_path    : override signature path; defaults to SIGNATURE_PATH
    """
    sig = Path(sig_path) if sig_path else SIGNATURE_PATH
    if not sig.exists():
        print(f"  [sig] No signature file found — skipping signature overlay")
        if esf_path != output_path:
            import shutil
            shutil.copy2(esf_path, output_path)
        return output_path

    date_str = _today_str()
    print(f"  [sig] Applying signature + date {date_str}")

    work_path = esf_path   # may be replaced by acroform-written copy

    # ── 1. Try to fill the AcroForm date field ────────────────────
    reader     = PdfReader(esf_path)
    all_fields = reader.get_fields() or {}
    date_field: str | None = None
    for name, fobj in all_fields.items():
        nl = name.lower()
        if fobj.get("/FT") == "/Tx":
            if any(k in nl for k in ("19b", "date_sign", "datesign",
                                     "sign_date", "signdate", "date_poa")):
                date_field = name
                break

    if date_field:
        print(f"  [sig] AcroForm date field: '{date_field}'")
        writer = PdfWriter()
        writer.append(reader)
        for page in writer.pages:
            try:
                writer.update_page_form_field_values(page, {date_field: date_str})
            except Exception:
                pass
        tmp = esf_path + "._sigtmp.pdf"
        with open(tmp, "wb") as fh:
            writer.write(fh)
        work_path = tmp

    # ── 2. Build image+date overlay via pdfplumber text anchors ───
    page_sizes: list[tuple[float, float]] = []
    sig_page_idx: int | None = None
    sig_anchors:  dict | None = None

    with pdfplumber.open(work_path) as pdf:
        for idx, page in enumerate(pdf.pages):
            pw, ph = _pw_ph(page)
            page_sizes.append((pw, ph))
            if sig_page_idx is None:
                anchors = _find_sig_anchors(page, ph)
                if anchors:
                    sig_page_idx = idx
                    sig_anchors  = anchors
                    print(f"  [sig] Section-VI anchors on page {idx + 1}")

    if sig_page_idx is None and page_sizes:
        # Fallback: place near the bottom of the last page
        sig_page_idx = len(page_sizes) - 1
        pw, ph = page_sizes[sig_page_idx]
        sig_anchors = {
            "sig_x":  72,  "sig_y":  90,
            "date_x": 350, "date_y": 90,
        }
        print("  [sig] Section-VI not found — using last-page fallback position")

    if sig_page_idx is None:
        print("  [sig] Empty ESF — skipping signature overlay")
        return output_path

    overlay_bytes  = _build_sig_overlay(
        page_sizes, sig_page_idx, sig_anchors, sig, date_str
    )
    orig_reader    = PdfReader(work_path)
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    writer         = PdfWriter()

    for i, orig_page in enumerate(orig_reader.pages):
        if i < len(overlay_reader.pages):
            # Overlay (signature + date) must be ON TOP of the original ESF.
            orig_page.merge_page(overlay_reader.pages[i])
            writer.add_page(orig_page)
        else:
            writer.add_page(orig_page)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as fh:
        writer.write(fh)
    print(f"  [sig] Written → {output_path}")

    if work_path.endswith("._sigtmp.pdf"):
        try:
            Path(work_path).unlink()
        except Exception:
            pass

    return output_path
