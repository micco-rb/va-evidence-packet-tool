import os
from pathlib import Path
from pypdf import PdfReader, PdfWriter


def get_metadata(pdf_path: str) -> dict:
    reader = PdfReader(pdf_path)
    meta = reader.metadata or {}
    return {
        "file": os.path.basename(pdf_path),
        "pages": len(reader.pages),
        "title": meta.get("/Title", "N/A"),
        "author": meta.get("/Author", "N/A"),
        "subject": meta.get("/Subject", "N/A"),
        "creator": meta.get("/Creator", "N/A"),
        "producer": meta.get("/Producer", "N/A"),
        "encrypted": reader.is_encrypted,
    }


def extract_text(pdf_path: str, page_numbers: list[int] | None = None) -> str:
    reader = PdfReader(pdf_path)
    pages = reader.pages

    if page_numbers:
        selected = []
        for n in page_numbers:
            if 1 <= n <= len(pages):
                selected.append(pages[n - 1])
            else:
                raise ValueError(f"Page {n} is out of range (1–{len(pages)})")
        pages = selected

    parts = []
    for i, page in enumerate(pages):
        text = page.extract_text() or ""
        parts.append(text)

    return "\n".join(parts)


def merge_pdfs(input_paths: list[str], output_path: str) -> int:
    writer = PdfWriter()
    total_pages = 0

    for path in input_paths:
        reader = PdfReader(path)
        for page in reader.pages:
            writer.add_page(page)
            total_pages += 1

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    return total_pages


def split_pdf(pdf_path: str, output_dir: str) -> list[str]:
    reader = PdfReader(pdf_path)
    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)

    stem = Path(pdf_path).stem
    created = []

    for i, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        out_path = output_dir_path / f"{stem}_page_{i:03d}.pdf"
        with open(out_path, "wb") as f:
            writer.write(f)
        created.append(str(out_path))

    return created


def extract_pages(pdf_path: str, page_numbers: list[int], output_path: str) -> int:
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    total = len(reader.pages)

    for n in page_numbers:
        if not (1 <= n <= total):
            raise ValueError(f"Page {n} is out of range (1–{total})")
        writer.add_page(reader.pages[n - 1])

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    return len(page_numbers)
