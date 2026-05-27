import os
import re
import requests
from bs4 import BeautifulSoup
from docx import Document
import pdfplumber

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter

# =====================================
# CONFIG
# =====================================

FILE_NAME = "attached_assets/sample.docx_1779808380311.docx"

# =====================================
# CONDITION NORMALIZATION TABLE
# Order matters — more specific patterns first
# =====================================

CONDITION_PATTERNS = [
    (r"obstructive sleep apnea|sleep apnea|OSA\b",          "Sleep Apnea"),
    (r"post.traumatic stress disorder|PTSD\b",              "PTSD"),
    (r"hypertension|high blood pressure",                   "Hypertension"),
    (r"diabetes mellitus.*type\s*[12ii]+|type\s*[12]\s*diabetes|diabetes mellitus|diabetes\b",
                                                             "Diabetes Mellitus"),
    (r"diabetic peripheral neuropathy|peripheral neuropathy|diabetic neuropathy|neuropathy\b",
                                                             "Neuropathy"),
    (r"tinnitus",                                           "Tinnitus"),
    (r"major depressive disorder|depression\b|MDD\b",       "Depression"),
    (r"generalized anxiety disorder|anxiety\b",             "Anxiety"),
    (r"gastroesophageal reflux|GERD\b|acid reflux",         "GERD"),
    (r"migraine headaches|migraines?\b",                    "Migraines"),
    (r"traumatic brain injury|TBI\b",                       "TBI"),
    (r"lumbar|lumbosacral|low back pain|back pain",         "Back Pain"),
    (r"knee\b",                                             "Knee Condition"),
    (r"shoulder\b",                                         "Shoulder Condition"),
    (r"hearing loss|hearing impairment",                    "Hearing Loss"),
]

# Noise phrases to strip from raw condition text before matching
_STRIP_SUFFIXES = re.compile(
    r"\s+(associated with|secondary to|due to|including as secondary to|as secondary to"
    r"|related to|caused by|aggravated by).*",
    re.IGNORECASE,
)

# Lines/phrases that signal a condition is being discussed as a *cause*
# rather than a *claimed* condition
_SECONDARY_SIGNALS = re.compile(
    r"service.connected\s+(?P<cond>[a-z ,]+?)\s*(,|and|;|\.|$)"
    r"|secondary to\s+(?P<cond2>[a-z ,]+?)\s*(,|and|;|\.|$)"
    r"|nexus to\s+(?P<cond3>[a-z ,]+?)\s*(,|and|;|\.|$)",
    re.IGNORECASE,
)

# =====================================
# PDF GENERATOR
# =====================================

def create_pdf(output_path, title, content_lines):
    doc = SimpleDocTemplate(output_path, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []
    elements.append(Paragraph(title, styles["Heading1"]))
    elements.append(Spacer(1, 20))
    for line in content_lines:
        elements.append(Paragraph(str(line), styles["BodyText"]))
        elements.append(Spacer(1, 10))
    doc.build(elements)
    print(f"Created PDF: {output_path}")


# =====================================
# DOCX EXTRACTION
# =====================================

def extract_docx_text(file_path):
    doc = Document(file_path)
    full_text = []
    for para in doc.paragraphs:
        full_text.append(para.text)
    return "\n".join(full_text)


# =====================================
# PDF EXTRACTION
# =====================================

def extract_pdf_text(file_path):
    full_text = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text.append(text)
    return "\n".join(full_text)


# =====================================
# URL EXTRACTION
# =====================================

def extract_urls(text):
    url_pattern = r"https?://[^\s\]\)]+"
    urls = re.findall(url_pattern, text)
    return list(set(urls))


# =====================================
# VETERAN NAME EXTRACTION
# =====================================

def extract_vet_name(text: str) -> str:
    """
    Extract the veteran's full name from the memorandum.

    Tries in order:
      1. "Veteran FirstName LastName" — standard body format
      2. "Last, First" — header block format (first 30 lines)
    """
    # Standard body pattern: "Veteran Anthony Novak"
    m = re.search(
        r"Veteran\s+([A-Z][a-zA-Z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-zA-Z]+)",
        text,
    )
    if m:
        return m.group(1)

    # Header block pattern: "Novak, Anthony" (Last, First on its own line)
    for line in text.split("\n")[:30]:
        stripped = line.strip()
        hm = re.match(r"^([A-Z][a-zA-Z\-']+),\s+([A-Z][a-zA-Z\-']+)$", stripped)
        if hm:
            return f"{hm.group(2)} {hm.group(1)}"   # → "Anthony Novak"

    return "Unknown Veteran"


# =====================================
# VA FILE NUMBER EXTRACTION
# =====================================

def extract_va_file_number(text: str) -> str:
    """
    Extract the veteran's VA File Number from memo or ESF text.

    Tries in order:
      1. Explicitly labelled  (VA File Number:, C-File No., etc.)
      2. Space-separated 9-digit  "321 469 459" or "321-469-459"
      3. Header-block scan  — a line in the first 30 lines that is
         ONLY digits (and optional spaces / dashes) totalling 7-10 digits,
         e.g. the line right below the veteran name
      4. Bare 9-digit starting with 0

    Returns the number with all non-digit characters stripped.
    """
    def _strip(raw: str) -> str:
        return re.sub(r"[^0-9]", "", raw)

    # 1. Labelled patterns (number may contain spaces)
    labelled = [
        r"VA\s+File\s+(?:No\.?|Number|#)\s*[:\-]?\s*([\d][\d\s\-]{5,13})",
        r"C[\-]?File\s+(?:No\.?|Number|#)\s*[:\-]?\s*([\d][\d\s\-]{5,13})",
        r"File\s+(?:No\.?|Number|#)\s*[:\-]?\s*([\d][\d\s\-]{5,13})",
        r"Claim\s+(?:No\.?|Number|#)\s*[:\-]?\s*([\d][\d\s\-]{5,13})",
        r"\bVA\s*#\s*([\d][\d\s\-]{5,13})\b",
    ]
    for pat in labelled:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = _strip(m.group(1))
            if 7 <= len(raw) <= 10:
                return raw

    # 2. Space/dash-separated 3-3-3 digit block  "321 469 459"
    m = re.search(r"\b(\d{3})[\s\-](\d{3})[\s\-](\d{3})\b", text)
    if m:
        return m.group(1) + m.group(2) + m.group(3)

    # 3. Header-block scan — lone digits-only line in first 30 lines
    for line in text.split("\n")[:30]:
        stripped = line.strip()
        digits   = _strip(stripped)
        # Accept lines that are ONLY digits, spaces, and dashes, 7-10 digits total
        if re.fullmatch(r"[\d\s\-]+", stripped) and 7 <= len(digits) <= 10:
            return digits

    # 4. Bare 9-digit starting with 0
    m = re.search(r"\b(0\d{8})\b", text)
    if m:
        return m.group(1)

    return ""


# =====================================
# CONDITION NORMALIZATION HELPER
# =====================================

def _normalize_condition(raw: str) -> str | None:
    """
    Strip noise suffixes then map to a canonical condition name.
    Returns canonical name or None if not recognized.
    """
    cleaned = _STRIP_SUFFIXES.sub("", raw).strip().rstrip(".,;:")
    for pattern, canonical in CONDITION_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return canonical
    return None


# =====================================
# SMART ILLNESS EXTRACTION
# =====================================

def extract_illnesses_smart(text: str) -> dict:
    """
    Extract only CLAIMED conditions from a VA memorandum.

    Returns a dict:
      {
        'raw_candidates': list of raw strings found,
        'methods':        list of extraction methods used,
        'illnesses':      deduplicated list of canonical condition names,
        'debug_log':      list of human-readable debug lines,
      }
    """
    debug: list[str] = []
    raw_candidates: list[str] = []
    methods: list[str] = []

    # ── Strategy 1: Memo title lines ──────────────────────────────────
    # VA memos follow: "Memorandum in Support of a Supplemental Claim"
    # followed by the condition name on the very next non-blank line.
    title_pattern = re.compile(
        r"Memorandum in Support of (?:a )?Supplemental Claim\s*\n+([^\n]{3,120})",
        re.IGNORECASE,
    )
    for m in title_pattern.finditer(text):
        raw = m.group(1).strip()
        debug.append(f"[memo-title] Found: \"{raw}\"")
        raw_candidates.append(raw)
        methods.append("memo_title")

    # ── Strategy 2: Explicit diagnosis / claimed-condition sections ───
    # Looks for lines like "Diagnosis:", "Claimed Condition:", etc.
    section_pattern = re.compile(
        r"(?:Diagnosis(?:es)?|Claimed Condition|Medical History|"
        r"Issue(?:s)? on Appeal|Contentions?)\s*[:\-]\s*([^\n]{3,120})",
        re.IGNORECASE,
    )
    for m in section_pattern.finditer(text):
        raw = m.group(1).strip()
        debug.append(f"[section-header] Found: \"{raw}\"")
        raw_candidates.append(raw)
        methods.append("section_header")

    # ── Strategy 3: Fallback — keyword scan of introduction section ──
    # Only runs if strategies 1 & 2 found nothing.
    if not raw_candidates:
        debug.append("[fallback] No memo titles or section headers found — scanning intro")
        # Take only first 2000 chars (introduction) to avoid secondary mentions
        intro = text[:2000]
        for _pat, canonical in CONDITION_PATTERNS:
            if re.search(_pat, intro, re.IGNORECASE):
                debug.append(f"[fallback] Keyword match: \"{canonical}\"")
                raw_candidates.append(canonical)
                methods.append("keyword_fallback")

    # ── Normalize & deduplicate ───────────────────────────────────────
    seen: set[str] = set()
    illnesses: list[str] = []

    for raw in raw_candidates:
        canonical = _normalize_condition(raw)
        if canonical is None:
            debug.append(f"[skip] Could not normalize: \"{raw}\"")
            continue
        if canonical in seen:
            debug.append(f"[dedup] Duplicate skipped: \"{canonical}\"")
            continue
        seen.add(canonical)
        illnesses.append(canonical)
        debug.append(f"[accept] \"{raw}\" → \"{canonical}\"")

    debug.append(f"[result] Final conditions: {illnesses}")

    return {
        "raw_candidates": raw_candidates,
        "methods": methods,
        "illnesses": illnesses,
        "debug_log": debug,
    }


# =====================================
# LEGACY ILLNESS EXTRACTION (kept for compatibility)
# =====================================

def extract_illnesses(text):
    illnesses = []
    possible_conditions = [
        "Hypertension", "Sleep Apnea", "PTSD", "Migraines",
        "GERD", "Tinnitus", "Depression", "Anxiety",
    ]
    for illness in possible_conditions:
        if illness.lower() in text.lower():
            illnesses.append(illness)
    return illnesses


# =====================================
# CREATE FOLDERS
# =====================================

def create_folders(vet_name, illnesses):
    os.makedirs(vet_name, exist_ok=True)
    for illness in illnesses:
        os.makedirs(os.path.join(vet_name, illness), exist_ok=True)


# =====================================
# COVER PAGE
# =====================================

def generate_cover_page(vet_name, illnesses, urls):
    file_path = os.path.join(vet_name, "Cover_Page.pdf")
    pdf_lines = [f"Veteran: {vet_name}", "", "Conditions Included:"]
    for illness in illnesses:
        pdf_lines.append(f"- {illness}")
    pdf_lines.append("")
    pdf_lines.append(f"Total Research Articles: {len(urls)}")
    create_pdf(file_path, "VA Medical Evidence Packet", pdf_lines)


# =====================================
# DOWNLOAD RESEARCH (legacy scraper)
# =====================================

def download_research_articles(vet_name, illnesses, urls):
    headers = {"User-Agent": "Mozilla/5.0"}
    for index, url in enumerate(urls, 1):
        try:
            response = requests.get(url, headers=headers, timeout=20)
            soup = BeautifulSoup(response.text, "html.parser")
            title_tag = soup.find("title")
            title = title_tag.text.strip() if title_tag else "Unknown Title"
            abstract = "No abstract found."
            abstract_section = soup.find("div", class_="abstract-content")
            if abstract_section:
                abstract = abstract_section.get_text(separator="\n").strip()
            combined_text = (title + " " + abstract).lower()
            matched_illnesses = [ill for ill in illnesses if ill.lower() in combined_text]
            if not matched_illnesses:
                matched_illnesses.append("General")
                os.makedirs(os.path.join(vet_name, "General"), exist_ok=True)
            pdf_links = []
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if ".pdf" in href.lower():
                    if href.startswith("/"):
                        href = "https://pubmed.ncbi.nlm.nih.gov" + href
                    pdf_links.append(href)
            for illness in matched_illnesses:
                illness_folder = os.path.join(vet_name, illness)
                os.makedirs(illness_folder, exist_ok=True)
                if pdf_links:
                    try:
                        pdf_url = pdf_links[0]
                        pdf_response = requests.get(pdf_url, headers=headers, timeout=20)
                        pdf_path = os.path.join(illness_folder, f"Original_Research_{index}.pdf")
                        with open(pdf_path, "wb") as pdf_file:
                            pdf_file.write(pdf_response.content)
                        print(f"Downloaded ORIGINAL PDF -> {illness}")
                    except Exception as pdf_error:
                        print(f"Could not download original PDF: {pdf_error}")
                summary_pdf_path = os.path.join(illness_folder, f"Research_Summary_{index}.pdf")
                pdf_lines = [f"Title: {title}", "", f"URL: {url}", "", "Abstract:", abstract]
                create_pdf(summary_pdf_path, title, pdf_lines)
                print(f"Saved summary PDF -> {illness}")
        except Exception as e:
            print(f"Failed: {url}\n{e}")


# =====================================
# MAIN
# =====================================

def main():
    if not os.path.exists(FILE_NAME):
        print(f"File not found: {FILE_NAME}")
        return

    if FILE_NAME.endswith(".docx"):
        text = extract_docx_text(FILE_NAME)
    elif FILE_NAME.endswith(".pdf"):
        text = extract_pdf_text(FILE_NAME)
    else:
        print("Unsupported file type.")
        return

    vet_name = extract_vet_name(text)
    result   = extract_illnesses_smart(text)
    illnesses = result["illnesses"]
    urls      = extract_urls(text)

    print("\n=== EXTRACTION DEBUG ===")
    for line in result["debug_log"]:
        print(" ", line)
    print(f"\nVeteran:   {vet_name}")
    print(f"Illnesses: {illnesses}")
    print(f"URLs:      {len(urls)}")

    create_folders(vet_name, illnesses)
    generate_cover_page(vet_name, illnesses, urls)
    download_research_articles(vet_name, illnesses, urls)

    print("\n========== COMPLETE ==========")
    print(f"Veteran: {vet_name}")
    print(f"Illnesses Found: {len(illnesses)}")
    print(f"Research URLs: {len(urls)}")
    print("\nAutomation completed successfully.")


if __name__ == "__main__":
    main()
