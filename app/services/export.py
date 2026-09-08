"""Render a Document's structured_data into pdf/docx/xlsx bytes."""

import io
from typing import Any

from docx import Document as DocxDocument
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

_PDF_PAGE_WIDTH = 612  # US Letter, points
_PDF_PAGE_HEIGHT = 792
_PDF_FONT_SIZE = 10
_PDF_LINE_HEIGHT = 14
_PDF_LEFT_MARGIN = 40
_PDF_TOP_MARGIN = _PDF_PAGE_HEIGHT - 50
_PDF_BOTTOM_MARGIN = 40


class ExportError(RuntimeError):
    """Raised when a document cannot be rendered in the requested format."""


def _structured_lines(source: str, structured_data: dict[str, Any] | None) -> list[str]:
    """Flatten structured_data into a simple list of text lines, used by
    the PDF renderer (and handy for debugging)."""
    lines: list[str] = [f"Source: {source}", ""]
    structured_data = structured_data or {}

    sections = structured_data.get("sections") or []
    if sections:
        lines.append("SECTIONS")
        for section in sections:
            lines.append(f"- {section.get('title', 'General')}")
            for content_line in str(section.get("content", "")).splitlines():
                if content_line.strip():
                    lines.append(f"    {content_line.strip()}")
        lines.append("")

    tables = structured_data.get("tables") or []
    if tables:
        lines.append("TABLES")
        for idx, table in enumerate(tables, start=1):
            lines.append(f"- Table {idx}")
            for row in table.get("rows", []):
                lines.append("    " + " | ".join(str(c) for c in row))
        lines.append("")

    if not sections and not tables:
        lines.append("(no structured data extracted)")

    return lines


def render_docx(source: str, structured_data: dict[str, Any] | None) -> bytes:
    structured_data = structured_data or {}
    doc = DocxDocument()
    doc.add_heading(source, level=1)

    sections = structured_data.get("sections") or []
    if sections:
        doc.add_heading("Sections", level=2)
        for section in sections:
            doc.add_heading(str(section.get("title", "General")), level=3)
            content = str(section.get("content", ""))
            if content:
                doc.add_paragraph(content)

    tables = structured_data.get("tables") or []
    if tables:
        doc.add_heading("Tables", level=2)
        for idx, table in enumerate(tables, start=1):
            doc.add_heading(f"Table {idx}", level=3)
            rows = table.get("rows") or []
            if not rows:
                continue
            n_cols = max(len(r) for r in rows)
            docx_table = doc.add_table(rows=0, cols=n_cols)
            docx_table.style = "Table Grid"
            for row in rows:
                cells = docx_table.add_row().cells
                for i in range(n_cols):
                    cells[i].text = row[i] if i < len(row) else ""

    if not sections and not tables:
        doc.add_paragraph("(no structured data extracted)")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def render_xlsx(source: str, structured_data: dict[str, Any] | None) -> bytes:
    structured_data = structured_data or {}
    wb = Workbook()

    sections_ws = wb.active
    sections_ws.title = "Sections"
    sections_ws.append(["Source", source])
    sections_ws.append([])
    sections_ws.append(["Title", "Content"])
    for section in structured_data.get("sections") or []:
        sections_ws.append([section.get("title", "General"), section.get("content", "")])

    tables = structured_data.get("tables") or []
    if tables:
        tables_ws = wb.create_sheet("Tables")
        for idx, table in enumerate(tables, start=1):
            tables_ws.append([f"Table {idx}"])
            for row in table.get("rows", []):
                tables_ws.append(row)
            tables_ws.append([])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap_line(line: str, max_chars: int = 100) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    return [line[i : i + max_chars] for i in range(0, len(line), max_chars)]


def render_pdf(source: str, structured_data: dict[str, Any] | None) -> bytes:
    raw_lines = _structured_lines(source, structured_data)
    lines: list[str] = []
    for line in raw_lines:
        lines.extend(_wrap_line(line) or [""])
    if not lines:
        lines = [""]

    max_lines_per_page = int((_PDF_TOP_MARGIN - _PDF_BOTTOM_MARGIN) / _PDF_LINE_HEIGHT)

    writer = PdfWriter()

    for page_start in range(0, len(lines), max_lines_per_page):
        page_lines = lines[page_start : page_start + max_lines_per_page]
        page = writer.add_blank_page(width=_PDF_PAGE_WIDTH, height=_PDF_PAGE_HEIGHT)

        content_ops = [
            "BT",
            f"/F1 {_PDF_FONT_SIZE} Tf",
            f"{_PDF_LINE_HEIGHT} TL",
            f"{_PDF_LEFT_MARGIN} {_PDF_TOP_MARGIN} Td",
        ]
        for i, text_line in enumerate(page_lines):
            if i > 0:
                content_ops.append("T*")
            content_ops.append(f"({_pdf_escape(text_line)}) Tj")
        content_ops.append("ET")
        stream_data = "\n".join(content_ops).encode("latin-1", errors="replace")

        stream_obj = DecodedStreamObject()
        stream_obj.set_data(stream_data)
        stream_ref = writer._add_object(stream_obj)  # noqa: SLF001

        font_obj = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_ref = writer._add_object(font_obj)  # noqa: SLF001

        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = stream_ref

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


_RENDERERS = {
    "pdf": render_pdf,
    "docx": render_docx,
    "xlsx": render_xlsx,
}

_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def render(format: str, source: str, structured_data: dict[str, Any] | None) -> bytes:
    try:
        renderer = _RENDERERS[format]
    except KeyError as exc:
        raise ExportError(f"Unsupported export format: {format!r}") from exc
    return renderer(source, structured_data)


def content_type_for(format: str) -> str:
    return _CONTENT_TYPES[format]
