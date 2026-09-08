"""Heuristic structure extraction: split OCR'd text into sections and tables.

v1 is intentionally simple:
  - a short line that is ALL CAPS or ends with ':' starts a new section -
    unless it (or the line right before it) contains a digit, since that's
    real lab-report evidence of a data row or a method-name annotation
    glued to one, not a heading (see _is_header_line)
  - consecutive lines that look column-aligned (pipe, tab, or 2+ runs of
    2+ spaces) are grouped into a table block

This is not meant to be a robust table/layout parser - just enough
structure to make exports useful.

Exception: for the native-PDF-text path, pdfplumber's own extract_tables()
runs on the real page objects in ocr_service (see
ocr_service/app/ocr.py::_try_native_pdf_text) and is passed in here as
`native_tables`. Native PDF text extraction collapses a table's visual
column spacing into single spaces, so the space-heuristic detector below
never fires on those rows - real, structured table data from pdfplumber is
used instead of the heuristic whenever it's available, since it's strictly
more reliable for that path. The OCR/rasterized path has no page object to
extract_tables() from and always falls back to the heuristic, unchanged.
"""

import logging
import re
from typing import Any

logger = logging.getLogger("sense_tool.structure")

_HEADER_MAX_LEN = 80
_MULTI_SPACE_RE = re.compile(r" {2,}")


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if "|" in stripped:
        return True
    if "\t" in stripped:
        return True
    return len(_MULTI_SPACE_RE.split(stripped)) >= 2


def _has_digit(s: str) -> bool:
    return any(c.isdigit() for c in s)


# A line adjacent to numeric data is very unlikely to be a real section
# heading in a lab report. Evidence (LabReport-1.pdf, pure space-heuristic
# path - no native tables): the all-caps rule alone turned every one of
# these into its own bogus 0-content section:
#   - short method-name annotations sitting directly under a result line,
#     e.g. "COLORIMETRIC" right after "Haemoglobin (Hb) * L 11.4* 12.0-15.0
#     g/dL", "CALCULATED" (7 separate times) after various result lines,
#     "ELECTRICAL IMPEDENCE", "FLOW CYTOMETRY" - these have zero digits
#     themselves but always immediately follow a line that has some.
#   - result rows whose own test-name abbreviation happens to be
#     all-uppercase, e.g. "RDW-CV H 15.7* 11.5-14.5 %" and "ATYPICAL CELLS
#     00" - these both have digits in themselves (the result value).
# Checked this doesn't cost real headings on the same document: "COMPLETE
# HEMOGRAM", "HRCT – CHEST", and "FINDINGS:" are each preceded by a
# digit-free line and contain no digits themselves, so all three still
# pass. Not evidenced either way: a genuine heading that happens to
# contain a digit (e.g. "COVID-19 SCREENING") would also be suppressed by
# this rule - no such case exists in the document this was checked
# against, so that's a known, unverified edge case, not a solved one.
def _is_header_line(line: str, previous_line: str | None) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _HEADER_MAX_LEN:
        return False
    if _has_digit(stripped) or (previous_line and _has_digit(previous_line)):
        return False
    if stripped.endswith(":"):
        return True
    letters = [c for c in stripped if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if "|" in stripped:
        cells = [c.strip() for c in stripped.split("|")]
    elif "\t" in stripped:
        cells = [c.strip() for c in stripped.split("\t")]
    else:
        cells = [c.strip() for c in _MULTI_SPACE_RE.split(stripped)]
    return [c for c in cells if c != ""]


# Mirrors ocr_service/app/ocr.py::_MIN_TABLE_ROWS/_MIN_TABLE_COLUMNS - see
# that module's comment for the real-document evidence behind these
# minimums (single-column "tables" there were confirmed to be misdetected
# paragraph text, not real tabular data).
_MIN_NATIVE_TABLE_ROWS = 2
_MIN_NATIVE_TABLE_COLUMNS = 2


def _is_well_formed_native_table(table: Any) -> bool:
    """Defensive re-check on this side of the HTTP boundary: ocr_service
    already drops ragged/empty/implausibly-shaped tables (see its own
    _is_well_formed_table), but don't trust that blindly - a malformed
    table here should never take down the whole document either."""
    if not isinstance(table, list) or not table or not isinstance(table[0], list):
        return False
    width = len(table[0])
    if width < _MIN_NATIVE_TABLE_COLUMNS or len(table) < _MIN_NATIVE_TABLE_ROWS:
        return False
    return all(isinstance(row, list) and len(row) == width for row in table)


# Mirrors ocr_service/app/ocr.py::_is_spurious_row - see that module's
# comment for the real-document evidence (a repeated page letterhead row
# vs. the legitimate single-cell "Differential Leucocyte Count"
# section-subheading row, both from the same real lab report).
def _is_spurious_native_row(row: Any) -> bool:
    non_empty = [c for c in row if c and str(c).strip()]
    if len(non_empty) == 0:
        return True
    if len(non_empty) == 1:
        return "\n" in str(non_empty[0])
    return False


def _convert_native_tables(native_tables: list) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for table in native_tables:
        if not _is_well_formed_native_table(table):
            logger.warning("dropping malformed native table from OCR service response")
            continue

        cleaned = [row for row in table if not _is_spurious_native_row(row)]
        if len(cleaned) < _MIN_NATIVE_TABLE_ROWS:
            logger.warning(
                "dropping native table: only %d row(s) left after removing spurious "
                "rows (below the %d-row floor)",
                len(cleaned),
                _MIN_NATIVE_TABLE_ROWS,
            )
            continue

        rows = [["" if cell is None else str(cell) for cell in row] for row in cleaned]
        converted.append(
            {
                "raw_lines": [" | ".join(row) for row in rows],
                "rows": rows,
                "source": "native_pdf",
            }
        )
    return converted


def parse_structure(text: str | None, native_tables: list | None = None) -> dict[str, Any]:
    """Return `{"sections": [...], "tables": [...]}` parsed from `text`.

    `native_tables` (optional): pdfplumber's raw extract_tables() output for
    a native-PDF-text document, one entry per table across all pages. When
    non-empty, these populate the returned `tables` directly instead of the
    space-heuristic detector (see module docstring). Section detection is
    unaffected either way - the heuristic never treated these rows as table
    lines in the first place (that's the bug this exists to work around),
    so skipping the heuristic's table-detection here doesn't change how
    section text is grouped.
    """
    lines = text.splitlines() if text else []
    use_native_tables = bool(native_tables)

    sections: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []

    current_title: str | None = None
    current_content: list[str] = []
    table_buffer: list[str] = []

    def flush_table() -> None:
        if table_buffer:
            tables.append(
                {
                    "raw_lines": list(table_buffer),
                    "rows": [_split_table_row(l) for l in table_buffer],
                    "source": "heuristic",
                }
            )
            table_buffer.clear()

    def flush_section() -> None:
        content = "\n".join(current_content).strip()
        if current_title is not None or content:
            sections.append({"title": current_title or "General", "content": content})
        current_content.clear()

    # Tracks the previous non-blank line's text, for _is_header_line's
    # digit-adjacency check. Reset on a blank line - a blank line is a
    # natural paragraph/page break, and letting the "previous line had a
    # digit" signal leak across one risked suppressing a genuine heading
    # that happens to open the next block (e.g. across this document's own
    # page boundaries, joined with a blank line - see
    # ocr_service/app/ocr.py's page-joining in _try_native_pdf_text).
    previous_line: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            previous_line = None
            continue
        if not use_native_tables and _is_table_line(line):
            table_buffer.append(line)
            previous_line = stripped
            continue
        flush_table()
        if _is_header_line(line, previous_line):
            flush_section()
            current_title = stripped.rstrip(":")
        else:
            current_content.append(stripped)
        previous_line = stripped

    flush_table()
    flush_section()

    if use_native_tables:
        tables = _convert_native_tables(native_tables)

    return {"sections": sections, "tables": tables}
