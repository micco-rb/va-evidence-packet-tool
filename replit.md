# PDF & Word Document Processing Tool

A Python CLI tool for common PDF and Word document automation tasks.

## Features

### PDF Commands
- **pdf-info** — display metadata (title, author, page count, encryption status)
- **pdf-extract** — pull text from all pages or specific pages, optionally save to a file
- **pdf-merge** — combine multiple PDFs into one
- **pdf-split** — break a PDF into individual single-page files
- **pdf-extract-pages** — copy specific pages into a new PDF

### Word / DOCX Commands
- **docx-info** — display metadata (title, author, paragraph count, dates)
- **docx-extract** — pull all text from a .docx file, optionally save to a file
- **docx-to-pdf** — convert a Word document to PDF

## Usage

```bash
# --- PDF ---

# Show info about a PDF
python main.py pdf-info document.pdf

# Extract all text and print it
python main.py pdf-extract document.pdf

# Extract text from pages 1, 3, and 5 and save to a file
python main.py pdf-extract document.pdf --pages 1,3,5 --output text.txt

# Merge PDFs
python main.py pdf-merge a.pdf b.pdf c.pdf --output combined.pdf

# Split into one file per page
python main.py pdf-split document.pdf --output-dir pages/

# Extract specific pages into a new PDF
python main.py pdf-extract-pages document.pdf 2,4,6 --output selected.pdf

# --- Word / DOCX ---

# Show info about a Word document
python main.py docx-info document.docx

# Extract all text and save to a file
python main.py docx-extract document.docx --output text.txt

# Convert a Word document to PDF
python main.py docx-to-pdf document.docx --output document.pdf
```

## Stack

- **pypdf** — PDF reading and writing
- **python-docx** — Word document reading
- **fpdf2** — PDF generation (used for docx-to-pdf)
- **typer** — CLI framework
- **rich** — terminal output formatting

## User preferences

- Python only, no frontend.
