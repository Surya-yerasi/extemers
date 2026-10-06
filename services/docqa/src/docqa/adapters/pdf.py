"""PDF text extraction and page rendering. Pure CPU; no AWS."""

import io
import re

import pypdfium2 as pdfium
from pypdf import PdfReader

_COLUMN_GAP = re.compile(r"\S\s{3,}\S")
_GAP_SPLIT = re.compile(r"\s{3,}")


def layout_to_markdown(text: str) -> str:
    """Turn layout-mode text into Markdown: runs of column-aligned lines become a table.

    pypdf's default mode emits one table cell per line, losing rows; layout mode keeps rows
    but as space-aligned columns. A line with two or more wide gaps is treated as a row.
    """
    out: list[str] = []
    table: list[list[str]] = []

    def flush() -> None:
        if len(table) >= 2:  # a single aligned line is just spacing, not a table
            width = max(len(r) for r in table)
            rows = [r + [""] * (width - len(r)) for r in table]
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + " --- |" * width)
            out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
        else:
            out.extend("   ".join(r) for r in table)
        table.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if len(_COLUMN_GAP.findall(stripped)) >= 2:
            table.append(_GAP_SPLIT.split(stripped))
            continue
        flush()
        out.append(re.sub(r"\s{2,}", " ", stripped))
    flush()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def extract_page_texts(data: bytes) -> list[str]:
    """Text layer of each page as Markdown ('' for scanned pages without one)."""
    reader = PdfReader(io.BytesIO(data))
    return [
        layout_to_markdown(page.extract_text(extraction_mode="layout") or "")
        for page in reader.pages
    ]


def render_page_png(data: bytes, page_index: int, scale: float = 2.0) -> bytes:
    """Render one page to PNG (scale 2.0 ≈ 144 dpi: legible for vision, small payload)."""
    pdf = pdfium.PdfDocument(data)
    try:
        image = pdf[page_index].render(scale=scale).to_pil()
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    finally:
        pdf.close()
