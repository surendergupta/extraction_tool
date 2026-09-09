import io
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image, ImageDraw, ImageFont

from app.main import app

pytestmark = pytest.mark.asyncio

_TESSERACT_AVAILABLE = shutil.which("tesseract") is not None


def _installed_langs() -> set[str]:
    if not _TESSERACT_AVAILABLE:
        return set()
    try:
        out = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    # `tesseract --list-langs` prints a header line then one code per line,
    # historically to stderr - merge both streams and drop the header.
    lines = (out.stdout + out.stderr).splitlines()
    return {ln.strip() for ln in lines if ln.strip() and "List of available" not in ln}


_INSTALLED_LANGS = _installed_langs()

# Lohit fonts are installed by the ocr_service Docker image purely so these
# tests (and the eval harness) can render real Gujarati/Devanagari glyphs
# into synthetic sample images. Outside that image they're usually absent,
# and the script-OCR tests below skip.
_GUJ_FONT = next(
    (
        p
        for p in [
            "/usr/share/fonts/truetype/lohit-gujarati/Lohit-Gujarati.ttf",
            "/usr/share/fonts/truetype/fonts-gujr-extra/Lohit-Gujarati.ttf",
        ]
        if Path(p).exists()
    ),
    None,
)
_DEVA_FONT = next(
    (
        p
        for p in [
            "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
            "/usr/share/fonts/truetype/lohit-nepali/Lohit-Nepali.ttf",
        ]
        if Path(p).exists()
    ),
    None,
)

_GUJ_RANGE = range(0x0A80, 0x0B00)  # Gujarati Unicode block
_DEVA_RANGE = range(0x0900, 0x0980)  # Devanagari Unicode block


def _count_in_range(text: str, code_range: range) -> int:
    return sum(1 for ch in text if ord(ch) in code_range)


def _png_with_script_lines(lines: list[str], font_path: str) -> bytes:
    """Render `lines` as large, high-contrast black-on-white text using a
    script-capable TrueType font - a deliberately clean, synthetic stand-in
    for a photographed multi-script document (real photos OCR far worse; the
    tests only assert the script is recognised *at all*, not accuracy)."""
    font = ImageFont.truetype(font_path, 44)
    width = 1000
    line_h = 70
    img = Image.new("RGB", (width, line_h * len(lines) + 40), color="white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((30, 20 + i * line_h), line, fill="black", font=font)
    scaled = img.resize((width * 2, img.height * 2), Image.LANCZOS)
    buf = io.BytesIO()
    scaled.save(buf, format="PNG")
    return buf.getvalue()


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


# --- WP-B: image/photo/chart region extraction -----------------------------

import base64  # noqa: E402


def _photo_like_png(w: int, h: int) -> bytes:
    """A smooth 2-D colour gradient - a continuous-tone stand-in for a photo
    or chart region (no text, high fill, high local variance)."""
    import numpy as np

    ys = np.linspace(0, 255, h, dtype=np.float32)[:, None]
    xs = np.linspace(0, 255, w, dtype=np.float32)[None, :]
    r = np.tile(xs, (h, 1))
    g = np.tile(ys, (1, w))
    b = (255 - 0.5 * (r + g)).clip(0, 255)
    arr = np.dstack([r, g, b]).astype("uint8")
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _page_png_with_text_and_photo() -> bytes:
    """A synthetic 'scanned page': white background, several lines of real
    text, and a pasted photo-like block in the lower area."""
    page = Image.new("RGB", (1000, 1300), "white")
    d = ImageDraw.Draw(page)
    for i in range(14):
        d.text((60, 60 + i * 34), f"PATIENT RECORD LINE NUMBER {i} - HELLO WORLD", fill="black")
    photo = Image.open(io.BytesIO(_photo_like_png(520, 460)))
    page.paste(photo, (240, 720))
    buf = io.BytesIO()
    page.save(buf, format="PNG")
    return buf.getvalue()


def _text_only_page_png() -> bytes:
    """Negative control: a page that is nothing but text."""
    page = Image.new("RGB", (1000, 1300), "white")
    d = ImageDraw.Draw(page)
    for i in range(30):
        d.text((60, 50 + i * 38), f"LINE {i:02d} THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 123", fill="black")
    buf = io.BytesIO()
    page.save(buf, format="PNG")
    return buf.getvalue()


def _native_pdf_with_text_and_embedded_image() -> bytes:
    """A PDF with a real text layer AND an embedded raster image - mirrors a
    lab report whose letterhead logo is a raster on an otherwise-typed page."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawImage(ImageReader(io.BytesIO(_photo_like_png(300, 120))), 60, 700, width=300, height=120)
    t = c.beginText(60, 660)
    for line in [
        "SREE DIAGNOSTIC CENTRE - HAEMATOLOGY REPORT",
        "Patient Name: John Matthew    Age/Sex: 47 / Male",
        "Haemoglobin 11.4 g/dL    Total Leucocyte Count 9600 /cu.mm",
        "Impression: Mild anaemia. Clinical correlation advised.",
    ]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return buf.getvalue()


def _decodes_to_image(b64: str) -> tuple[int, int]:
    im = Image.open(io.BytesIO(base64.b64decode(b64)))
    im.load()
    return im.size


# --- WP-D: ruled-line table region detection (OCR-fallback path) ------------


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)      # Pillow >= 10: scalable default
    except TypeError:                                   # very old Pillow
        return ImageFont.load_default()


def _png_ruled_table(rows: list[list[str]], cell_w: int = 300, cell_h: int = 60) -> bytes:
    """A synthetic scanned page: a heading line, then a fully ruled grid
    table (drawn lines + text). Mirrors what the ruled-line detector is for."""
    ncols = max(len(r) for r in rows)
    w = 100 + ncols * cell_w
    h = 260 + len(rows) * cell_h
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 36), "MEDICAL EXAMINATION REPORT", fill="black", font=_font(26))
    body = _font(24)
    x0, y0 = 50, 160
    for i in range(len(rows) + 1):
        d.line((x0, y0 + i * cell_h, x0 + ncols * cell_w, y0 + i * cell_h), fill="black", width=2)
    for j in range(ncols + 1):
        d.line((x0 + j * cell_w, y0, x0 + j * cell_w, y0 + len(rows) * cell_h), fill="black", width=2)
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            d.text((x0 + j * cell_w + 12, y0 + i * cell_h + 16), cell, fill="black", font=body)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_colored_chart_box() -> bytes:
    """A bordered box filled with saturated green/yellow/red bands - the
    DEXA reference-chart false-positive pattern the FP filter must reject."""
    import numpy as np

    w, h = 900, 380
    arr = np.full((h, w, 3), 255, np.uint8)
    bands = [(60, 200, 60), (240, 230, 40), (220, 40, 40)]
    for k, col in enumerate(bands):
        arr[40 + k * 110:40 + (k + 1) * 110, 40:w - 40] = col
    img = Image.fromarray(arr)
    d = ImageDraw.Draw(img)
    d.rectangle((30, 30, w - 30, h - 30), outline="black", width=3)
    for gx in range(120, w - 40, 120):
        d.line((gx, 40, gx, h - 40), fill="black", width=1)
    d.text((45, 5), "Reference: AP Spine L1-L4", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_form_field_grid() -> bytes:
    """A small, wide grid of many boxed label:value fields - the Gulf
    CANDIDATE INFORMATION false-positive pattern the FP filter must reject.
    ~5 rows x 6 cols of short label/value cells."""
    labels = [
        ["Name", "JOHN", "Age", "36", "Marital", "MARRIED"],
        ["Son of", "ROBERT", "Gender", "MALE", "Nationality", "INDIAN"],
        ["Place", "CITY", "Weight", "72", "Passport", "A0000000"],
        ["Post", "TECH", "Height", "167", "Visa Date", "-"],
        ["Issue", "2023", "Expiry", "2025", "Visa No", "-"],
    ]
    return _png_ruled_table(labels, cell_w=200, cell_h=54)


async def test_ocr_native_pdf_has_no_table_regions(client):
    """Native-PDF path: `table_regions` is always empty and text_source
    says so. The ruled-line detector is OCR-fallback-path only."""
    pdf_bytes = _pdf_with_table()
    resp = await client.post("/ocr", files={"file": ("r.pdf", pdf_bytes, "application/pdf")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text_source"] == "native_pdf"
    assert body["table_regions"] == []
    assert len(body["tables"]) == 1                     # native grid unchanged


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_scanned_ruled_table_returns_region(client):
    """A scanned page with a ruled table -> one `ruled_line_region` entry:
    a bbox + a whole-region OCR text blob, NO grid. `tables` stays empty
    (that's the native-PDF field)."""
    png = _png_ruled_table([
        ["Type of Examination", "Result"],
        ["Haemoglobin", "11.4 g/dL"],
        ["Blood Pressure", "120/80"],
        ["Total Leucocyte Count", "9600"],
        ["Platelet Count", "2.15 lakh"],
    ])
    resp = await client.post("/ocr", files={"file": ("scan.png", png, "image/png")})
    assert resp.status_code == 200
    body = resp.json()

    assert body["text_source"] == "ocr"
    assert body["tables"] == []                          # native-grid field, unused here
    assert len(body["table_regions"]) >= 1
    tr = body["table_regions"][0]
    assert tr["source"] == "ruled_line_region"
    assert "region_text" in tr and "rows" not in tr      # a blob, not a grid
    x0, y0, x1, y1 = tr["bbox"]
    assert 0 <= x0 < x1 and 0 <= y0 < y1
    blob = tr["region_text"].lower()
    assert "haemoglobin" in blob and ("120/80" in blob or "9600" in blob)


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_ruled_line_fp_filter_suppresses_colored_chart(client):
    """FP filter (WP-D): a bordered, strongly-coloured multi-hue box (a
    reference-chart pattern) must NOT be returned as a table region."""
    png = _png_colored_chart_box()
    resp = await client.post("/ocr", files={"file": ("chart.png", png, "image/png")})
    assert resp.status_code == 200
    assert resp.json()["table_regions"] == []


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_ruled_line_fp_filter_suppresses_form_field_grid(client):
    """FP filter (WP-D): a small, wide grid of many boxed label:value fields
    (the CANDIDATE INFORMATION pattern) must NOT be returned as a table."""
    png = _png_form_field_grid()
    resp = await client.post("/ocr", files={"file": ("form.png", png, "image/png")})
    assert resp.status_code == 200
    assert resp.json()["table_regions"] == []


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_text_only_page_no_table_regions(client):
    """Negative control: a text-only page yields no ruled-line table
    regions (no rules to detect)."""
    resp = await client.post(
        "/ocr", files={"file": ("t.png", _text_only_page_png(), "image/png")}
    )
    assert resp.status_code == 200
    assert resp.json()["table_regions"] == []


async def test_ocr_native_pdf_returns_embedded_images(client):
    """The native-PDF path must return each embedded raster image with a
    PDF-points bbox and real, decodable bytes - not empty/corrupt data."""
    pdf_bytes = _native_pdf_with_text_and_embedded_image()
    resp = await client.post("/ocr", files={"file": ("report.pdf", pdf_bytes, "application/pdf")})
    assert resp.status_code == 200
    body = resp.json()

    assert "HAEMATOLOGY REPORT" in body["text"]        # text extraction unaffected
    assert len(body["images"]) >= 1
    img = body["images"][0]
    assert img["source"] == "embedded"
    assert img["bbox_space"] == "pdf_points"
    assert img["region_type_guess"] in {"photo", "chart", "logo", "stamp", "unknown"}
    w, h = _decodes_to_image(img["image_base64"])
    assert w > 0 and h > 0
    x0, y0, x1, y1 = img["bbox"]
    assert 0 <= x0 < x1 and 0 <= y0 < y1                # sane box


async def test_ocr_extract_images_false_disables_image_output(client):
    pdf_bytes = _native_pdf_with_text_and_embedded_image()
    resp = await client.post(
        "/ocr",
        params={"extract_images": "false"},
        files={"file": ("report.pdf", pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["images"] == []
    assert "HAEMATOLOGY REPORT" in body["text"]         # still extracted


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_scanned_page_detects_photo_region(client):
    """A rasterised page with a photo-like block among text: the block is
    detected and returned as a 'detected' image region; text still extracted."""
    png = _page_png_with_text_and_photo()
    resp = await client.post("/ocr", files={"file": ("scan.png", png, "image/png")})
    assert resp.status_code == 200
    body = resp.json()

    assert "HELLO" in body["text"].upper()             # text extraction unaffected
    detected = [i for i in body["images"] if i["source"] == "detected"]
    assert detected, "expected at least one detected image region"

    # at least one region overlaps where the block was pasted (x 240..760, y 720..1180)
    def _overlaps(b):
        x0, y0, x1, y1 = b
        return x1 > 200 and x0 < 800 and y1 > 680 and y0 < 1220

    assert any(_overlaps(i["bbox"]) for i in detected)
    for i in detected:
        assert i["bbox_space"] == "page_pixels"
        w, h = _decodes_to_image(i["image_base64"])
        assert w > 0 and h > 0


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_text_only_page_detects_no_image_regions(client):
    """Negative control: a text-only page must not yield any detected image
    region (a text block wrongly returned as an image would be lost to any
    downstream text consumer)."""
    png = _text_only_page_png()
    resp = await client.post("/ocr", files={"file": ("text.png", png, "image/png")})
    assert resp.status_code == 200
    body = resp.json()
    assert "QUICK BROWN FOX" in body["text"].upper()
    assert [i for i in body["images"] if i["source"] == "detected"] == []


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
    assert body["table_regions"] == []
    assert body["text_source"] == "native_pdf"


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
    assert body["text_source"] == "ocr"
    assert body["table_regions"] == []                  # plain text, no ruled table


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


# --- lang param (multi-script OCR) -------------------------------------------


async def test_ocr_rejects_malformed_lang_param(client):
    """The `lang` value goes onto the Tesseract command line, so its shape is
    constrained to 3-letter codes joined by '+'. Anything else is a 422 from
    request validation, before any OCR runs (no tesseract needed)."""
    png_bytes = _png_with_text("HELLO")
    for bad in ("english", "eng;ls", "eng+", "e", "eng ", "../eng"):
        response = await client.post(
            "/ocr",
            params={"lang": bad},
            files={"file": ("scan.png", png_bytes, "image/png")},
        )
        assert response.status_code == 422, f"{bad!r} should be rejected"


async def test_ocr_rejects_uninstalled_lang_pack(client):
    """A `lang` that passes the shape check but names a pack not baked into
    the image is a clean 400 - NOT a silent downgrade. Tesseract given
    `eng+zzz` with no zzz.traineddata just drops zzz and runs as `eng`
    (verified); the endpoint refuses that ambiguity explicitly."""
    png_bytes = _png_with_text("HELLO")
    response = await client.post(
        "/ocr", params={"lang": "eng+zzz"}, files={"file": ("scan.png", png_bytes, "image/png")}
    )
    assert response.status_code == 400
    assert "zzz" in response.json()["detail"]


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_lang_omitted_is_unchanged_english_behaviour(client):
    """Omitting `lang` must behave exactly like the pre-existing English-only
    path: same request shape, English text still recognised."""
    png_bytes = _png_with_text("HELLO WORLD")
    response = await client.post("/ocr", files={"file": ("scan.png", png_bytes, "image/png")})
    assert response.status_code == 200
    assert "HELLO" in response.json()["text"].upper()


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_ocr_explicit_eng_matches_default(client):
    """`lang=eng` explicitly must produce the same output as omitting it."""
    png_bytes = _png_with_text("HELLO WORLD")
    default = await client.post("/ocr", files={"file": ("s.png", png_bytes, "image/png")})
    explicit = await client.post(
        "/ocr", params={"lang": "eng"}, files={"file": ("s.png", png_bytes, "image/png")}
    )
    assert default.status_code == explicit.status_code == 200
    assert default.json()["text"] == explicit.json()["text"]


@pytest.mark.skipif(
    not (_TESSERACT_AVAILABLE and "guj" in _INSTALLED_LANGS and _GUJ_FONT),
    reason="tesseract, guj tessdata, or a Gujarati font missing",
)
async def test_ocr_eng_guj_recognises_gujarati_script(client):
    """Realistic-synthetic: a Gujarati hospital letterhead with an English
    body line (mirroring the described real sample), OCR'd in combined
    `eng+guj` mode. Assert only that the Gujarati script is recognised at
    all - non-empty, enough characters land in the Gujarati Unicode block,
    and no Devanagari leaks in. Not exact accuracy, and not the English
    line (Lohit-Gujarati's Latin glyphs OCR poorly - a font artefact of the
    synthetic render, not the combined mode; see ocr_service/README.md)."""
    png_bytes = _png_with_script_lines(
        ["સિવિલ હોસ્પિટલ અમદાવાદ", "દર્દીનું નામ અને ઉંમર", "Blood Report - Haemoglobin"],
        _GUJ_FONT,
    )
    response = await client.post(
        "/ocr", params={"lang": "eng+guj"}, files={"file": ("letterhead.png", png_bytes, "image/png")}
    )
    assert response.status_code == 200
    text = response.json()["text"]
    assert text.strip(), "combined-mode OCR returned nothing"
    guj_chars = _count_in_range(text, _GUJ_RANGE)
    assert guj_chars >= 5, f"expected Gujarati-script output, got {text!r}"
    # Devanagari must NOT leak in from the guj model on Gujarati input.
    assert _count_in_range(text, _DEVA_RANGE) == 0, f"unexpected Devanagari in {text!r}"


@pytest.mark.skipif(
    not (_TESSERACT_AVAILABLE and "nep" in _INSTALLED_LANGS and _DEVA_FONT),
    reason="tesseract, nep tessdata, or a Devanagari font missing",
)
async def test_ocr_eng_nep_recognises_devanagari_script(client):
    """Realistic-synthetic: a Nepali medical form with an English body line,
    OCR'd in combined `eng+nep` mode. Assert only that the Devanagari script
    is recognised at all (non-empty, enough characters in the Devanagari
    Unicode block) - not exact accuracy."""
    png_bytes = _png_with_script_lines(
        ["त्रिभुवन विश्वविद्यालय अस्पताल", "बिरामीको नाम र ठेगाना", "Blood Report - Haemoglobin"],
        _DEVA_FONT,
    )
    response = await client.post(
        "/ocr", params={"lang": "eng+nep"}, files={"file": ("form.png", png_bytes, "image/png")}
    )
    assert response.status_code == 200
    text = response.json()["text"]
    assert text.strip(), "combined-mode OCR returned nothing"
    deva_chars = _count_in_range(text, _DEVA_RANGE)
    assert deva_chars >= 5, f"expected Devanagari-script output, got {text!r}"


# --- WP-G: POST /searchable-pdf ------------------------------------------
# Standalone endpoint (WP-G two-pass architecture): its own Tesseract pass
# (`--oem 1 --psm 6`) on the ORIGINAL image, returning raw application/pdf.
# Every marker string / image below is fabricated in this file - none of it
# is derived from any real evaluated document.


def _pdf_page_texts(pdf_bytes: bytes) -> list[str]:
    """Text layer of each page of a generated PDF, via pdfplumber."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(page.extract_text() or "") for page in pdf.pages]


_DEJAVU = next(
    (p for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"] if Path(p).exists()),
    None,
)


def _png_lines_dejavu(lines: list[str]) -> bytes:
    """Several lines of large, crisp black-on-white text in a real scalable
    font (DejaVu) - OCRs reliably, unlike Pillow's tiny bitmap default."""
    font = ImageFont.truetype(_DEJAVU, 40)
    w, line_h = 1400, 66
    img = Image.new("RGB", (w, line_h * len(lines) + 40), "white")
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((40, 20 + i * line_h), ln, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _two_page_scanned_pdf(marker_a: str, marker_b: str) -> bytes:
    """A 2-page PDF, each page nothing but an embedded raster of one marker
    word - i.e. a scanned document with no text layer."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for marker in (marker_a, marker_b):
        c.drawImage(
            ImageReader(io.BytesIO(_png_with_text(marker))), 72, 650, width=400, height=80
        )
        c.showPage()
    c.save()
    return buf.getvalue()


async def test_searchable_pdf_rejects_empty_file(client):
    resp = await client.post(
        "/searchable-pdf", files={"file": ("scan.png", b"", "image/png")}
    )
    assert resp.status_code == 400


async def test_searchable_pdf_rejects_malformed_lang(client):
    """`lang` shape is regex-validated before any Tesseract work."""
    png_bytes = _png_with_text("HELLO")
    resp = await client.post(
        "/searchable-pdf",
        params={"lang": "english"},
        files={"file": ("scan.png", png_bytes, "image/png")},
    )
    assert resp.status_code == 422


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_searchable_pdf_from_scanned_image_is_searchable(client):
    """A scanned image -> application/pdf whose invisible text layer is
    extractable and carries the page's words."""
    png_bytes = _png_with_text("SENTINELWORD")
    resp = await client.post(
        "/searchable-pdf", files={"file": ("scan.png", png_bytes, "image/png")}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:4] == b"%PDF"

    pages = _pdf_page_texts(resp.content)
    assert len(pages) == 1
    assert "SENTINEL" in pages[0].upper(), f"text layer missing marker: {pages[0]!r}"


@pytest.mark.skipif(not _TESSERACT_AVAILABLE, reason="tesseract binary not installed")
async def test_searchable_pdf_multipage_preserves_page_order(client):
    pdf_in = _two_page_scanned_pdf("SENTINELALPHA", "SENTINELBRAVO")
    resp = await client.post(
        "/searchable-pdf", files={"file": ("scan.pdf", pdf_in, "application/pdf")}
    )
    assert resp.status_code == 200
    pages = _pdf_page_texts(resp.content)
    assert len(pages) == 2
    assert "SENTINELALPHA" in pages[0].upper().replace(" ", "")
    assert "SENTINELBRAVO" in pages[1].upper().replace(" ", "")


@pytest.mark.skipif(
    not (_TESSERACT_AVAILABLE and _DEJAVU), reason="tesseract or DejaVu font missing"
)
async def test_searchable_pdf_pinned_psm6_keeps_label_value_rows_on_one_line(client):
    """Regression for the A/B eval finding: with the pinned `--psm 6` pass,
    each 'LABEL ... VALUE' row stays on ONE text line in the invisible layer
    (psm 3 fragmented such wide rows into disjoint column blocks)."""
    rows = [
        "ALPHAFIELD                              AVALUE",
        "BRAVOFIELD                              BVALUE",
        "CHARLIEFIELD                            CVALUE",
    ]
    resp = await client.post(
        "/searchable-pdf",
        files={"file": ("grid.png", _png_lines_dejavu(rows), "image/png")},
    )
    assert resp.status_code == 200
    out_lines = [ln.upper() for ln in _pdf_page_texts(resp.content)[0].splitlines()]
    alpha = next((ln for ln in out_lines if "ALPHAFIELD" in ln), None)
    assert alpha is not None, f"row label not recognised at all: {out_lines!r}"
    assert "AVALUE" in alpha, f"psm-6 row split across lines: {alpha!r}"


@pytest.mark.skipif(
    not (_TESSERACT_AVAILABLE and "guj" in _INSTALLED_LANGS and _GUJ_FONT),
    reason="tesseract, guj tessdata, or a Gujarati font missing",
)
async def test_searchable_pdf_gujarati_invisible_layer_is_unicode(client):
    """`lang=eng+guj`: the generated PDF's invisible text layer must be real
    Gujarati Unicode (selectable/searchable), not mojibake. Fabricated
    generic phrase - a common greeting, not from any document."""
    png_bytes = _png_with_script_lines(["નમસ્તે આરોગ્ય કેન્દ્ર", "તપાસ અહેવાલ"], _GUJ_FONT)
    resp = await client.post(
        "/searchable-pdf",
        params={"lang": "eng+guj"},
        files={"file": ("scan.png", png_bytes, "image/png")},
    )
    assert resp.status_code == 200
    joined = "".join(_pdf_page_texts(resp.content))
    assert _count_in_range(joined, _GUJ_RANGE) >= 5, f"no Gujarati in text layer: {joined!r}"
    assert _count_in_range(joined, _DEVA_RANGE) == 0


@pytest.mark.skipif(
    not (_TESSERACT_AVAILABLE and "nep" in _INSTALLED_LANGS and _DEVA_FONT),
    reason="tesseract, nep tessdata, or a Devanagari font missing",
)
async def test_searchable_pdf_devanagari_invisible_layer_is_unicode(client):
    """`lang=eng+nep`: invisible text layer must be real Devanagari Unicode.
    Fabricated generic greeting phrase."""
    png_bytes = _png_with_script_lines(["नमस्ते स्वास्थ्य केन्द्र", "जाँच प्रतिवेदन"], _DEVA_FONT)
    resp = await client.post(
        "/searchable-pdf",
        params={"lang": "eng+nep"},
        files={"file": ("scan.png", png_bytes, "image/png")},
    )
    assert resp.status_code == 200
    assert _count_in_range("".join(_pdf_page_texts(resp.content)), _DEVA_RANGE) >= 5
