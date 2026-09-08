"""English-only OCR core: native PDF text first, Tesseract (LSTM) otherwise.

Deliberately minimal for v1: Tesseract is excellent for clean, printed
English and is native-CPU/lightweight. Heavier engines (PaddleOCR fallback,
LayoutParser, Camelot, spaCy post-processing) are NOT included here -
add them only once real documents prove Tesseract-only accuracy is
insufficient, not speculatively.
"""

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image

from app.preprocess import preprocess_image

logger = logging.getLogger("ocr_service.ocr")

# Native PDF text extraction is essentially free and lossless where it
# applies, so it's always tried before rasterizing + OCR-ing a PDF.
_NATIVE_TEXT_MIN_CHARS = 20

_DEFAULT_PSM = 6  # "assume a single uniform block of text"
_OEM = 1  # LSTM engine only - the fast, English-tuned model in Tesseract 5

# A pdfplumber table is List[List[Optional[str]]] (rows of cells, cells
# nullable). NativeTable keeps that raw shape; the caller (Sense_tool's
# structure-parsing step) decides how to fold it into structured_data.
NativeTable = list[list[str | None]]


@dataclass
class OcrResult:
    text: str
    # Only ever non-empty for the native-PDF-text path - the OCR/rasterized
    # path has no pdfplumber page object to call extract_tables() on.
    tables: list[NativeTable] = field(default_factory=list)


class OCRError(RuntimeError):
    """Raised when text extraction fails."""


def _tesseract_config(psm: int) -> str:
    return f"--oem {_OEM} --psm {psm} -l eng"


def extract_text_from_bytes(filename: str, data: bytes, psm: int = _DEFAULT_PSM) -> OcrResult:
    if not data:
        raise OCRError("Empty file")

    suffix = Path(filename).suffix.lower()
    is_pdf = suffix == ".pdf" or data[:4] == b"%PDF"

    try:
        if is_pdf:
            native_text, native_tables = _try_native_pdf_text(data)
            if native_text is not None:
                return OcrResult(text=native_text, tables=native_tables)
            return OcrResult(text=_extract_pdf_via_ocr(data, psm))
        return OcrResult(text=_extract_image(data, psm))
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


def _try_native_pdf_text(data: bytes) -> tuple[str | None, list[NativeTable]]:
    """Return (embedded PDF text, tables) directly, skipping OCR entirely,
    when the PDF has a real text layer (i.e. it isn't just a scanned
    image). Tables come from the same pdfplumber page objects used for
    text - the page isn't discarded once text is pulled off it.

    Uses pdfplumber (MIT-licensed) rather than PyMuPDF (AGPLv3) - Sense_tool
    has no legal review clearing AGPLv3 for production/commercial use.
    """
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_texts: list[str] = []
            tables: list[NativeTable] = []
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
        return None, []

    combined = "\n\n".join(t for t in page_texts if t)
    if len(combined) >= _NATIVE_TEXT_MIN_CHARS:
        return combined, tables
    return None, []


def _extract_image(data: bytes, psm: int) -> str:
    pil_img = Image.open(io.BytesIO(data)).convert("RGB")
    cv_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    processed = preprocess_image(cv_img)
    return pytesseract.image_to_string(processed, config=_tesseract_config(psm))


def _extract_pdf_via_ocr(data: bytes, psm: int) -> str:
    from pdf2image import convert_from_bytes

    pages = convert_from_bytes(data)
    texts = []
    for page in pages:
        cv_img = cv2.cvtColor(np.array(page.convert("RGB")), cv2.COLOR_RGB2BGR)
        processed = preprocess_image(cv_img)
        texts.append(pytesseract.image_to_string(processed, config=_tesseract_config(psm)))
    return "\n\n".join(texts)
