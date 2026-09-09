"""OCR core: native PDF text first, Tesseract (LSTM) otherwise.

English-only by default. The Tesseract rasterize+OCR path also accepts a
combined language string (`eng+guj`, `eng+nep`) for single-pass
multi-script recognition - opt-in per request via the /ocr `lang` param,
never the default (see README.md for the measured English-accuracy
tradeoff). The tessdata for every supported code is baked into the Docker
image (see ../Dockerfile).

Deliberately minimal for v1: Tesseract is native-CPU/lightweight. Heavier
engines (PaddleOCR fallback, LayoutParser, Camelot, spaCy post-processing)
are NOT included here - add them only once real documents prove
Tesseract-only accuracy is insufficient, not speculatively.
"""

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image

from app.images import ExtractedImage, extract_native_pdf_images, images_from_page_array
from app.preprocess import preprocess_image
from app.tables import RuledTableRegion, detect_ruled_table_regions

logger = logging.getLogger("ocr_service.ocr")

# Native PDF text extraction is essentially free and lossless where it
# applies, so it's always tried before rasterizing + OCR-ing a PDF.
_NATIVE_TEXT_MIN_CHARS = 20

_DEFAULT_PSM = 6  # "assume a single uniform block of text"
_OEM = 1  # LSTM engine only - the fast neural line-recognizer in Tesseract 5

# Tesseract accepts a single language ("eng") or a "+"-joined combination
# ("eng+guj") for multi-script recognition in one pass. The default stays
# English-only so existing callers see byte-for-byte identical behaviour;
# combined modes are strictly opt-in per request (see README.md for the
# measured English-accuracy tradeoff that drove that choice). Each code
# here must have matching tessdata baked into the image (see Dockerfile).
_DEFAULT_LANG = "eng"

# A pdfplumber table is List[List[Optional[str]]] (rows of cells, cells
# nullable). NativeTable keeps that raw shape; the caller (Sense_tool's
# structure-parsing step) decides how to fold it into structured_data.
NativeTable = list[list[str | None]]


@dataclass
class OcrResult:
    text: str
    # NATIVE-PDF path only: pdfplumber extract_tables() output - a real
    # grid, list[list[cell]] per table. Always [] on the rasterize+OCR path.
    tables: list[NativeTable] = field(default_factory=list)
    # RASTERIZE+OCR path only (WP-D): ruled-line table *regions*. Each is a
    # bbox + a whole-region OCR text blob - NO grid (see app/tables.py).
    # Always [] on the native-PDF path. `tables` and `table_regions` are
    # deliberately different shapes; downstream branches on the entry's
    # `source` ("native_pdf" vs "ruled_line_region") and must not assume a
    # uniform shape.
    table_regions: list["RuledTableRegion"] = field(default_factory=list)
    # Which path produced `text`: "native_pdf" or "ocr". The structure
    # parser uses this to decide whether the legacy space-heuristic table
    # detector may run (native only - it is pure garbage on OCR text, per
    # WP-C/WP-D, and is replaced there by `table_regions`).
    text_source: str = "ocr"
    # WP-B: extracted image/photo/chart regions. "embedded" ones come from
    # pdfplumber's page.images on the native path; "detected" ones come from
    # the heuristic region detector on the rasterise+OCR path. Purely
    # additive - text/tables above are unaffected by this. See app/images.py.
    images: list["ExtractedImage"] = field(default_factory=list)


class OCRError(RuntimeError):
    """Raised when text extraction fails."""


def _tesseract_config(psm: int, lang: str = _DEFAULT_LANG) -> str:
    return f"--oem {_OEM} --psm {psm} -l {lang}"


def extract_text_from_bytes(
    filename: str,
    data: bytes,
    psm: int = _DEFAULT_PSM,
    lang: str = _DEFAULT_LANG,
    extract_images: bool = True,
) -> OcrResult:
    if not data:
        raise OCRError("Empty file")

    suffix = Path(filename).suffix.lower()
    is_pdf = suffix == ".pdf" or data[:4] == b"%PDF"

    try:
        if is_pdf:
            native_text, native_tables, native_images = _try_native_pdf_text(data, extract_images)
            if native_text is not None:
                # Native PDF text is the PDF's own Unicode text layer - it is
                # already script-correct regardless of `lang`, which only
                # affects the Tesseract rasterize+OCR path below.
                return OcrResult(
                    text=native_text,
                    tables=native_tables,
                    images=native_images,
                    text_source="native_pdf",
                )
            text, images, table_regions = _extract_pdf_via_ocr(data, psm, lang, extract_images)
            return OcrResult(
                text=text, images=images, table_regions=table_regions, text_source="ocr"
            )
        text, images, table_regions = _extract_image(data, psm, lang, extract_images)
        return OcrResult(
            text=text, images=images, table_regions=table_regions, text_source="ocr"
        )
    except OCRError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize to a domain error
        raise OCRError(f"OCR failed for {filename}: {exc}") from exc


# Minimum shape for a pdfplumber extract_tables() result to be trusted as a
# real table rather than a misdetected paragraph-text block.
#
# Evidence (checked against a real 4-page lab report, LabReport-1.pdf,
# where the default vertical/horizontal_strategy="lines" found 4 "tables"
# but only 1 was genuine):
#   - genuine tables (the Haematology results grid, split across pages 1-2
#     by the PDF's own pagination): 17 rows x 5 cols, and 12 rows x 5 cols.
#   - false positives (pages 3-4, pure paragraph text - an HRCT findings
#     block and a disclaimer/header block - bounded by decorative
#     horizontal rules, no real column structure): 2 rows x 1 col, and
#     3 rows x 1 col.
#
# Column count is the clean, decisive signal here: every false positive
# collapsed to exactly 1 column (no internal vertical structure at all),
# every genuine table had 5. Row count alone doesn't discriminate in this
# document (one false positive has 2 rows, matching the row minimum below),
# but a >=2 floor is still worth keeping as a cheap general safeguard
# against a degenerate single-row match.
#
# Tuning pdfplumber's own table_settings first (vertical/horizontal
# "lines_strict", edge_min_length up to 200) was tried and rejected:
# lines_strict finds ZERO tables on every page of this document, including
# the genuine one - its grid is drawn with rects/curves, not stroked line
# objects, so lines_strict has nothing to detect. Raising edge_min_length
# never removed the false positives (the page's own outer content-box
# rect is long enough to register as a table boundary at any tested
# length up to 200) while it started silently dropping real columns from
# the genuine table around edge_min_length=150 (12x5 -> 12x3). Neither
# knob is a clean fix here - this post-extraction shape filter is.
#
# Residual limitation, honestly: a real table drawn with only horizontal
# rules and no vertical column dividers (column separation via text
# alignment alone) already returns 0 tables from pdfplumber's default
# "lines" strategy *before* this filter ever runs - confirmed against a
# constructed test case. This filter can't fix that (nothing to filter -
# extract_tables() never found it), and precision here is bought at that
# pre-existing recall cost, not a new one this filter introduces.
_MIN_TABLE_ROWS = 2
_MIN_TABLE_COLUMNS = 2


def _is_well_formed_table(table: list) -> bool:
    """Reject empty/ragged tables (merged cells, malformed detection) and
    - per the evidence above - tables too small/narrow to plausibly be
    real tabular data rather than a misdetected text block."""
    if not table or not table[0]:
        return False
    width = len(table[0])
    if width < _MIN_TABLE_COLUMNS or len(table) < _MIN_TABLE_ROWS:
        return False
    return all(len(row) == width for row in table)


# Row-level counterpart to the table-level filter above: an otherwise
# genuine, multi-column table can still pick up one spurious row.
#
# Evidence (same LabReport-1.pdf, table 1 on page 2 - 12 rows x 5 cols,
# passes every check above since 11 of its 12 rows are real lab results):
# row 0 is ['Lab No. : ... UHID : ...\nPatient Name : ...\nAge/Sex :
# ...\nOPD/IPD No. : ...\nDoctor : ...', None, None, None, None] - the
# page's repeated patient-info letterhead banner, sitting only in column 0
# because it's one wide block of text with no columnar structure of its
# own, landing inside the table's detected bounding box. It has 4 embedded
# newlines and 266 characters.
#
# Before writing a rule, checked whether any *genuine* row has the same
# "only one populated cell" shape, since that alone isn't safe to drop on:
# table 0 (page 1, the other genuine table) has one - "Differential
# Leucocyte Count", alone in column 0. That's a real, legitimate row: it's
# a section-subheading inside the results table (the Neutrophil/
# Lymphocyte/Eosinophils/etc. rows immediately below it are its
# breakdown - a standard lab-report convention), not noise, and it must
# stay. It has 0 newlines and 28 characters.
#
# 0 vs 4 embedded newlines is the clean, decisive split between those two
# real examples, so that's the rule: a row with exactly one populated cell
# is only dropped when that cell's text spans multiple lines (a dense,
# multi-field block); a short single-line label in an otherwise-empty row
# is kept. A row with two or more populated cells is never touched by this
# rule regardless of how many *other* cells are empty - most genuine rows
# on this document have blank Status/Reference-Interval cells, and that's
# real, sparse data, not noise.
#
# Honestly: the newline count has to be >=1 to trigger a drop because
# that's the only threshold this document's two examples actually
# distinguish (0 vs 4) - there's no evidence here to calibrate a more
# lenient bar (e.g. tolerating one soft-wrapped line before dropping), so
# a genuinely long single-cell row that happens to wrap onto a second line
# would also be caught by this rule. Noted, not solved - no such row exists
# in this document to check the tradeoff against.
def _is_spurious_row(row: list) -> bool:
    non_empty = [c for c in row if c and str(c).strip()]
    if len(non_empty) == 0:
        return True  # a fully blank row carries no information either way
    if len(non_empty) == 1:
        return "\n" in non_empty[0]
    return False


def _drop_spurious_rows(table: NativeTable) -> NativeTable:
    return [row for row in table if not _is_spurious_row(row)]


def _try_native_pdf_text(
    data: bytes, extract_images: bool = True
) -> tuple[str | None, list[NativeTable], list[ExtractedImage]]:
    """Return (embedded PDF text, tables, embedded images) directly, skipping
    OCR entirely, when the PDF has a real text layer (i.e. it isn't just a
    scanned image). Tables and images come from the same pdfplumber page
    objects used for text - the page isn't discarded once text is pulled
    off it.

    Uses pdfplumber (MIT-licensed) rather than PyMuPDF (AGPLv3) - Sense_tool
    has no legal review clearing AGPLv3 for production/commercial use.
    """
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_texts: list[str] = []
            tables: list[NativeTable] = []
            images: list[ExtractedImage] = []
            if extract_images:
                try:
                    images = extract_native_pdf_images(pdf)
                except Exception as exc:  # noqa: BLE001 - never let image extraction break text
                    logger.warning("native embedded-image extraction failed: %s", exc)
            for page in pdf.pages:
                page_texts.append((page.extract_text() or "").strip())

                try:
                    page_tables = page.extract_tables() or []
                except Exception as exc:  # noqa: BLE001 - one page's tables shouldn't sink the doc
                    logger.warning("page.extract_tables() failed on one page: %s", exc)
                    page_tables = []

                for table in page_tables:
                    if not _is_well_formed_table(table):
                        width = len(table[0]) if table and table[0] else 0
                        logger.warning(
                            "dropping table detection: %d rows x %d cols (empty, ragged, "
                            "or below the %d-row/%d-col plausibility floor - likely a "
                            "misdetected text block, not a real table)",
                            len(table),
                            width,
                            _MIN_TABLE_ROWS,
                            _MIN_TABLE_COLUMNS,
                        )
                        continue

                    cleaned = _drop_spurious_rows(table)
                    if len(cleaned) < len(table):
                        logger.info(
                            "dropped %d spurious row(s) (blank, or a single dense "
                            "multi-line cell - e.g. a repeated page letterhead) from "
                            "an otherwise genuine %d-row table",
                            len(table) - len(cleaned),
                            len(table),
                        )
                    if len(cleaned) < _MIN_TABLE_ROWS:
                        logger.warning(
                            "dropping table entirely: only %d row(s) left after removing "
                            "spurious rows (below the %d-row floor)",
                            len(cleaned),
                            _MIN_TABLE_ROWS,
                        )
                        continue

                    tables.append(cleaned)
    except Exception:
        return None, [], []

    combined = "\n\n".join(t for t in page_texts if t)
    if len(combined) >= _NATIVE_TEXT_MIN_CHARS:
        return combined, tables, images
    return None, [], []


def _detect_images_safe(cv_img: np.ndarray, page: int) -> list[ExtractedImage]:
    """Region detection must never break text extraction - swallow anything."""
    try:
        return images_from_page_array(cv_img, page=page)
    except Exception as exc:  # noqa: BLE001
        logger.warning("image region detection failed on page %d: %s", page, exc)
        return []


def _extract_image(
    data: bytes, psm: int, lang: str = _DEFAULT_LANG, extract_images: bool = True
) -> tuple[str, list[ExtractedImage], list[RuledTableRegion]]:
    pil_img = Image.open(io.BytesIO(data)).convert("RGB")
    cv_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    images = _detect_images_safe(cv_img, 0) if extract_images else []
    # WP-D: ruled-line table regions - OCR-fallback path only. Safe by
    # contract (returns [] on any failure); never blocks text extraction.
    table_regions = detect_ruled_table_regions(cv_img, page=0)
    processed = preprocess_image(cv_img)
    text = pytesseract.image_to_string(processed, config=_tesseract_config(psm, lang))
    return text, images, table_regions


def _extract_pdf_via_ocr(
    data: bytes, psm: int, lang: str = _DEFAULT_LANG, extract_images: bool = True
) -> tuple[str, list[ExtractedImage], list[RuledTableRegion]]:
    from pdf2image import convert_from_bytes

    pages = convert_from_bytes(data)
    texts = []
    images: list[ExtractedImage] = []
    table_regions: list[RuledTableRegion] = []
    for i, page in enumerate(pages):
        cv_img = cv2.cvtColor(np.array(page.convert("RGB")), cv2.COLOR_RGB2BGR)
        if extract_images:
            images.extend(_detect_images_safe(cv_img, i))
        table_regions.extend(detect_ruled_table_regions(cv_img, page=i))
        processed = preprocess_image(cv_img)
        texts.append(pytesseract.image_to_string(processed, config=_tesseract_config(psm, lang)))
    return "\n\n".join(texts), images, table_regions
