"""WP-D (reduced scope): ruled-line table-region detection for the
rasterize+OCR path only.

This REPLACES the old space-heuristic table detector on the OCR-fallback
path (WP-C/WP-D evaluations measured that heuristic as pure garbage on
scanned pages - 1-12 bogus "tables" per page, real tables never
reconstructed). The native-PDF path (pdfplumber `extract_tables()`) is
untouched.

Scope is deliberately narrow, per the WP-D evaluation's recommendation:

  1. Find ruled-table bounding boxes via morphology (long thin horizontal
     and vertical structuring elements -> line masks -> intersections).
  2. OCR each surviving region as ONE block (`--psm 6`). NOT cell-by-cell -
     WP-D showed small-crop OCR is a net accuracy loss vs whole-region OCR.
  3. NO grid / row / column reconstruction - WP-D showed it is unreliable
     on any table with borderless internal rows (i.e. most real tables).
     The output is `{bbox, region_text}` - a text blob, not a grid.
  4. A false-positive filter (`_looks_like_data_table`) rejects coloured /
     continuous-tone regions (chart borders, photo strips) and small wide
     "boxed form-field grid" regions (the Gulf-employment CANDIDATE
     INFORMATION block), which the raw detector otherwise flags.

KNOWN LIMITATION (not addressed here): borderless / whitespace-aligned
tables - e.g. the monospace column layout of a typical lab report - have no
rules to detect and are invisible to this approach. Confirmed on a real
sample in WP-D. That gap is documented, not fixed, in this WP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract

from app.images import _colorfulness  # reuse the WP-B Hasler colourfulness metric

logger = logging.getLogger("ocr_service.tables")

_WORK_MAX = 2200                 # detection working resolution (matches WP-D eval)
_MIN_TABLE_AREA_FRAC = 0.01      # a candidate must cover >=1% of the page
_MIN_TABLE_W = 60
_MIN_TABLE_H = 40
_MAX_REGIONS = 8
_DESKEW_MAX_DEG = 15.0
_REGION_OCR_CONFIG = "--oem 1 --psm 6 -l eng"   # whole-region OCR only


@dataclass
class RuledTableRegion:
    """One detected ruled-table region. `region_text` is a whole-region OCR
    blob - there is deliberately NO row/column grid (see module docstring).
    `bbox` is [x0, y0, x1, y1] in pixels of the page image passed to
    `detect_ruled_table_regions` (same convention as app.images)."""

    bbox: list[int]
    region_text: str
    page: int = 0

    def to_payload(self) -> dict:
        return {
            "bbox": [int(v) for v in self.bbox],
            "page": self.page,
            "region_text": self.region_text,
            "source": "ruled_line_region",
        }


# --------------------------------------------------------------- preprocess
def _prep(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """-> (deskewed colour, deskewed gray, deskewed binary, scale-vs-original).

    Same recipe as app/preprocess.py (grayscale -> Otsu -> deskew), applied
    here to the colour and gray images too so line detection and region OCR
    all run on a straightened page."""
    h, w = bgr.shape[:2]
    scale = _WORK_MAX / max(h, w) if max(h, w) > _WORK_MAX else 1.0
    if scale != 1.0:
        bgr = cv2.resize(bgr, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    binar = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    coords = np.column_stack(np.where(binar < 255))
    angle = 0.0
    if coords.shape[0] >= 50:
        a = cv2.minAreaRect(coords)[-1]
        a = -(90 + a) if a < -45 else -a
        if 0.1 <= abs(a) <= _DESKEW_MAX_DEG:
            angle = a
    if angle:
        hh, ww = binar.shape
        m = cv2.getRotationMatrix2D((ww // 2, hh // 2), angle, 1.0)
        kw = dict(flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        binar = cv2.warpAffine(binar, m, (ww, hh), **kw)
        gray = cv2.warpAffine(gray, m, (ww, hh), **kw)
        bgr = cv2.warpAffine(bgr, m, (ww, hh), **kw)
    return bgr, gray, binar, scale


# ---------------------------------------------------------- line detection
def _detect_lines(binar: np.ndarray):
    inv = 255 - binar
    h, w = inv.shape
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 30), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 30)))
    horiz = cv2.morphologyEx(inv, cv2.MORPH_OPEN, hk, iterations=1)
    vert = cv2.morphologyEx(inv, cv2.MORPH_OPEN, vk, iterations=1)
    # bridge small gaps (dashed / broken photocopy rules)
    horiz = cv2.dilate(horiz, cv2.getStructuringElement(cv2.MORPH_RECT, (max(6, w // 120), 1)))
    vert = cv2.dilate(vert, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(6, h // 120))))
    joints = cv2.bitwise_and(horiz, vert)
    return horiz, vert, joints


def _line_positions(mask: np.ndarray, axis: int) -> list[int]:
    """Collapse a line mask to clustered centre positions along `axis`
    (0 = horizontal lines -> y positions, 1 = vertical -> x positions)."""
    proj = (mask > 0).sum(axis=1 - axis)
    if not proj.size or proj.max() == 0:
        return []
    on = proj > proj.max() * 0.3
    pos, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            pos.append((i + j) // 2)
            i = j
        else:
            i += 1
    return pos


# ------------------------------------------------------ false-positive filter
def _looks_like_data_table(color_roi: np.ndarray, n_rows: int, n_cols: int) -> tuple[bool, str]:
    """Reject the false-positive patterns WP-D's evaluation found the raw
    detector tripping on. Returns (keep, reason_if_rejected).

    Evidence / test cases (WP-D report):
      * DEXA reference-chart borders  -> coloured, multi-hue.
      * DEXA 6-panel body-scan strip  -> coloured + almost no paper-white.
      * Gulf CANDIDATE INFORMATION    -> a small, wide grid of many similar
        boxed label:value fields (recon ~8 rows x 8 cols), NOT a data table.

    TRADEOFF (honest): the form-grid rule keys on shape alone (no
    cell-content analysis), so a genuinely small, wide, fully-ruled data
    table (<= ~12 rows AND >= 5 columns) would also be suppressed. None
    exists in the WP-D sample set; a tall table (many rows) or a narrow one
    (<= 4 columns) is unaffected. The colour rule is tuned to pass the pale
    single-hue background tint of a coloured results table (WP-D DEXA
    reports: colourfulness ~45, saturation ~60) and reject only a
    strongly multi-hue chart (colourfulness > 100, saturation > 95 on the
    same samples) - a real table printed with a vivid, saturated fill could
    still be suppressed.
    """
    gray = cv2.cvtColor(color_roi, cv2.COLOR_BGR2GRAY)
    sat = cv2.cvtColor(color_roi, cv2.COLOR_BGR2HSV)[:, :, 1]
    mean_sat = float(sat.mean())
    cf = _colorfulness(color_roi)
    white_frac = float((gray > 245).mean())

    if cf > 60.0 and mean_sat > 75.0:
        return False, f"strongly coloured/multi-hue (colourfulness={cf:.0f}, sat={mean_sat:.0f}) - likely a chart"
    if white_frac < 0.20:
        return False, f"almost no paper-white ({white_frac:.2f}) - likely a photo/scan strip"
    if n_cols >= 5 and n_rows <= n_cols + 3 and n_rows <= 12:
        return False, f"small wide boxed grid ({n_rows}x{n_cols}) - likely a form-field block, not a data table"
    return True, ""


# ------------------------------------------------------------------- public
def detect_ruled_table_regions(bgr: np.ndarray, page: int = 0) -> list[RuledTableRegion]:
    """Detect ruled-table regions in one page image and OCR each as a block.
    Never raises - returns [] on any internal failure (the caller must not
    let table detection break text extraction)."""
    try:
        return _detect(bgr, page)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ruled-line table detection failed on page %d: %s", page, exc)
        return []


def _detect(bgr: np.ndarray, page: int) -> list[RuledTableRegion]:
    color, gray, binar, scale = _prep(bgr)
    H, W = gray.shape
    page_area = H * W
    horiz, vert, joints = _detect_lines(binar)

    grid = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((7, 7), np.uint8))
    cnts, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < _MIN_TABLE_AREA_FRAC * page_area or w < _MIN_TABLE_W or h < _MIN_TABLE_H:
            continue
        nj = cv2.connectedComponentsWithStats((joints[y:y + h, x:x + w] > 0).astype(np.uint8))[0] - 1
        rows = _line_positions(horiz[y:y + h, x:x + w], axis=0)
        cols = _line_positions(vert[y:y + h, x:x + w], axis=1)
        if len(rows) < 2 or len(cols) < 2 or nj < 4:
            continue
        keep, reason = _looks_like_data_table(color[y:y + h, x:x + w], len(rows) + 1, len(cols) + 1)
        if not keep:
            logger.info(
                "ruled-line: dropping %dx%d candidate at %s - %s",
                len(rows) + 1, len(cols) + 1, [x, y, w, h], reason,
            )
            continue
        candidates.append((x, y, w, h))

    # Paint the detected rules white before OCR: crisp grid lines break
    # Tesseract's "uniform block" assumption at --psm 6 (a synthetic
    # sharp-ruled table can OCR to nothing otherwise); on real faint-ruled
    # scans this is a harmless cleanup.
    rule_mask = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8))
    gray_deruled = gray.copy()
    gray_deruled[rule_mask > 0] = 255

    candidates.sort(key=lambda b: -(b[2] * b[3]))
    out: list[RuledTableRegion] = []
    for x, y, w, h in candidates[:_MAX_REGIONS]:
        pad = 3
        crop = gray_deruled[max(0, y + pad):y + h - pad, max(0, x + pad):x + w - pad]
        if crop.size == 0 or min(crop.shape) < 8:
            continue
        crop = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)
        text = pytesseract.image_to_string(crop, config=_REGION_OCR_CONFIG).strip()
        # A genuine ruled table's region OCRs to plenty of text; a near-empty
        # result means we boxed an empty ruled cell / a chart frame / rules
        # with no readable content - not worth emitting.
        if len(text.replace(" ", "").replace("\n", "")) < 12:
            continue
        out.append(
            RuledTableRegion(
                bbox=[int(x / scale), int(y / scale), int((x + w) / scale), int((y + h) / scale)],
                region_text=text,
                page=page,
            )
        )
    return out
