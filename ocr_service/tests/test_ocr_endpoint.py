import io
import shutil

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw

from app.main import app

pytestmark = pytest.mark.asyncio

_TESSERACT_AVAILABLE = shutil.which("tesseract") is not None


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _png_with_text(text: str) -> bytes:
    img = Image.new("RGB", (400, 80), color="white")
    ImageDraw.Draw(img).text((10, 20), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _simple_pdf_with_text(text: str) -> bytes:
    """A PDF with a real, extractable text layer (built with reportlab,
    BSD-licensed - test-only, not a runtime dependency of the service)."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


def _pdf_with_table() -> bytes:
    """A native-PDF-text document with a real, grid-lined table - reportlab
    (test-only) renders actual ruled lines, which is what pdfplumber's
    default extract_tables() strategy detects."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    data = [
        ["Test Name", "Status", "Result", "Reference Interval", "Unit"],
        ["Haemoglobin", "L", "11.4", "12.0-15.0", "g/dL"],
        ["WBC", "", "9.6", "4-10", "10^3/mm3"],
    ]
    table = Table(data)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    doc.build([table])
    return buf.getvalue()


def _pdf_with_paragraph_misdetected_as_table() -> bytes:
    """Reproduces a real false-positive pattern found on LabReport-1.pdf: a
    bordered content box (a rect) containing plain paragraph text with an
    internal horizontal rule, but NO vertical column dividers at all.
    pdfplumber's default extract_tables() still finds "a table" here (its
    outer rect + the horizontal rule are enough structure), but it collapses
    to exactly 1 column - real tabular data never does that on this
    document (see ocr_service/app/ocr.py's _MIN_TABLE_COLUMNS comment)."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(60, 550, 480, 200, stroke=1, fill=0)
    c.line(60, 680, 540, 680)
    c.setFont("Helvetica", 10)
    text1 = c.beginText(72, 700)
    for line in [
        "FINDINGS: Multiple fibrotic and fibrocalcific opacities are seen in",
        "the upper lobes of bilateral lungs with tree-in-bud appearance noted.",
    ]:
        text1.textLine(line)
    c.drawText(text1)
    text2 = c.beginText(72, 650)
    for line in [
        "Note:- This report IS NOT valid for medico-legal purposes.",
        "Please contact the front desk within 7 days of receiving the report.",
    ]:
        text2.textLine(line)
    c.drawText(text2)
    c.save()
    return buf.getvalue()


def _pdf_with_letterhead_row_and_subheading_row() -> bytes:
    """Reproduces a real row-level false-positive found on LabReport-1.pdf's
    table 1: a genuine, multi-column results table where the page's
    repeated patient-info letterhead banner lands as an extra row 0 (one
    dense, multi-line cell spanning all columns - vertical dividers don't
    cross that row's y-range, only the data rows'). Also includes a
    legitimate single-cell subheading row ("Differential Count", mirroring
    the real document's "Differential Leucocyte Count" row) to check the
    filter doesn't remove that too - short, single-line, no embedded
    newline, unlike the letterhead block."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(60, 300, 440, 400)
    c.line(60, 650, 500, 650)
    c.line(200, 300, 200, 650)  # column dividers only span the data-row area
    c.line(350, 300, 350, 650)
    c.line(60, 567, 500, 567)
    c.line(60, 483, 500, 483)
    c.line(60, 400, 500, 400)

    c.setFont("Helvetica", 9)
    header = c.beginText(70, 685)
    for line in ["Lab No.: 555 UHID: AB/1", "Patient Name: Jane Doe", "Doctor: Dr. Smith"]:
        header.textLine(line)
    c.drawText(header)

    for name, status, result, y in [
        ("Haemoglobin", "L", "11.4", 605),
        ("TLC", "", "9.6", 522),
        ("RBC Count", "", "4.5", 438),
    ]:
        c.drawString(65, y, name)
        c.drawString(210, y, status)
        c.drawString(360, y, result)

    c.drawString(65, 355, "Differential Count")
    c.save()
    return buf.getvalue()


def _scanned_pdf_from_png(png_bytes: bytes) -> bytes:
    """A PDF containing only an embedded raster image - no text layer,
    same as a real scanned document."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawImage(ImageReader(io.BytesIO(png_bytes)), 72, 650, width=400, height=80)
    c.save()
    return buf.getvalue()


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ocr_rejects_empty_file(client):
    response = await client.post("/ocr", files={"file": ("empty.png", b"", "image/png")})
    assert response.status_code == 400


async def test_ocr_native_pdf_skips_rasterization(client):
    """A PDF with an embedded text layer is read directly via pdfplumber -
    this must work even without Tesseract installed."""
    pdf_bytes = _simple_pdf_with_text("Native PDF text extraction works.")
    response = await client.post("/ocr", files={"file": ("report.pdf", pdf_bytes, "application/pdf")})
    assert response.status_code == 200
    body = response.json()
    assert "Native PDF text extraction works." in body["text"]
    # no table on this document - must not report a false positive
    assert body["tables"] == []


async def test_ocr_native_pdf_with_table_returns_tables(client):
    """A native-PDF-text document with a real (grid-lined) table must have
    that table extracted via pdfplumber's extract_tables(), not just its
    text - this is the fix for tables_detected coming back 0 on documents
    like real lab reports with results tables (the space-heuristic detector
    never fires on native-PDF text, since it collapses column spacing)."""
    pdf_bytes = _pdf_with_table()
    response = await client.post("/ocr", files={"file": ("report.pdf", pdf_bytes, "application/pdf")})
    assert response.status_code == 200
    body = response.json()

    assert "Haemoglobin" in body["text"]  # text extraction is unaffected

    assert len(body["tables"]) == 1
    rows = body["tables"][0]
    assert rows[0] == ["Test Name", "Status", "Result", "Reference Interval", "Unit"]
    assert rows[1][0] == "Haemoglobin"
    assert rows[1][2] == "11.4"
    assert rows[2] == ["WBC", "", "9.6", "4-10", "10^3/mm3"]


async def test_ocr_native_pdf_paragraph_text_not_misdetected_as_table(client):
    """Regression test for the false-positive fix: a real single-column
    "table" detection (paragraph text bounded by a rect + a rule, no
    vertical dividers - the exact pattern found on 3 of 4 pages of a real
    lab report) must be filtered out, not reported as structured data."""
    pdf_bytes = _pdf_with_paragraph_misdetected_as_table()
    response = await client.post("/ocr", files={"file": ("report.pdf", pdf_bytes, "application/pdf")})
    assert response.status_code == 200
    body = response.json()
    assert "FINDINGS" in body["text"]  # text extraction still works
    assert body["tables"] == []  # but it must not be reported as a table


async def test_ocr_native_pdf_drops_letterhead_row_keeps_subheading_and_data_rows(client):
    """Row-level regression test: within an otherwise-genuine table, a
    spurious multi-line letterhead row must be dropped, a legitimate
    single-cell subheading row must survive, and all genuine multi-column
    data rows must survive untouched."""
    pdf_bytes = _pdf_with_letterhead_row_and_subheading_row()
    response = await client.post("/ocr", files={"file": ("report.pdf", pdf_bytes, "application/pdf")})
    assert response.status_code == 200
    body = response.json()

    assert len(body["tables"]) == 1
    rows = body["tables"][0]
    assert len(rows) == 4  # 5 detected rows minus the dropped letterhead row
    assert not any("Lab No." in (cell or "") for row in rows for cell in row)

    assert rows[0] == ["Haemoglobin", "L", "11.4"]
    assert rows[1] == ["TLC", "", "9.6"]
    assert rows[2] == ["RBC Count", "", "4.5"]
    # legitimate single-cell subheading row - kept, unlike the letterhead
    assert rows[3] == ["Differential Count", "", ""]


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_image_extracts_text(client):
    png_bytes = _png_with_text("HELLO WORLD")
    response = await client.post("/ocr", files={"file": ("scan.png", png_bytes, "image/png")})
    assert response.status_code == 200
    body = response.json()
    assert "HELLO" in body["text"].upper()
    # images never have a pdfplumber page object to extract_tables() from
    assert body["tables"] == []


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_scanned_pdf_falls_back_to_tesseract(client):
    """A PDF with no text layer (rendered as an image) must fall back to
    rasterize + OCR rather than returning empty native text."""
    png_bytes = _png_with_text("SCANNED PAGE")
    pdf_bytes = _scanned_pdf_from_png(png_bytes)

    response = await client.post(
        "/ocr", files={"file": ("scanned.pdf", pdf_bytes, "application/pdf")}
    )
    assert response.status_code == 200
    body = response.json()
    assert "SCANNED" in body["text"].upper()
    # rasterized/OCR path has no pdfplumber page object - must never report tables
    assert body["tables"] == []
