"""WP-B: embedded image / photo / chart region detection + extraction.

Two independent paths, deliberately kept separate (they share nothing but
the best-effort type classifier):

1. NATIVE PDF  - pdfplumber already knows where every embedded raster image
   is (`page.images`, with a real bbox). We just pull the bytes out of the
   PDF stream and hand them back. High precision, essentially free.

2. SCANNED / RASTERISED  - the whole page is one image and there is no
   "embedded image" list. We have to *detect* photo/chart-like sub-regions
   and tell them apart from text and ruled tables. This is a genuinely hard
   CV problem and the detector here is a heuristic, honestly partial one -
   see `detect_regions` and ocr_service/README.md for its measured
   behaviour (what it catches, what it misses, what it false-positives on).

Scope is detection + extraction only. Nothing here touches text/table
extraction, and nothing is wired into export - the caller just gets the
crops and their boxes so a later work package can use them.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger("ocr_service.images")

# --- output caps -----------------------------------------------------------
# Keep the JSON response sane: a full-page scan can yield multi-MB crops.
_MAX_REGIONS = 12
_MAX_RETURN_DIM = 1600          # downscale a returned crop's long edge to this
_MIN_REGION_PX = 40             # ignore anything thinner than this on a side
_JPEG_QUALITY = 85

REGION_TYPES = ("photo", "chart", "logo", "stamp", "unknown")


@dataclass
class ExtractedImage:
    """One extracted image region.

    bbox is [x0, y0, x1, y1] in the coordinate space of `bbox_space`:
      - "pdf_points" for native-PDF embedded images (page-relative, PDF
        points, top-left origin) - `page` says which page.
      - "page_pixels" for detected regions (pixels of the rasterised page
        image the detector ran on) - `page` is the page index for
        multi-page PDFs, else 0.
    `source` is "embedded" (path 1) or "detected" (path 2).
    `region_type_guess` is a LOW-CONFIDENCE best-effort label, never a claim.
    """

    bbox: list[float]
    image_bytes: bytes
    region_type_guess: str = "unknown"
    source: str = "detected"
    bbox_space: str = "page_pixels"
    page: int = 0
    width: int = 0
    height: int = 0
    image_format: str = "png"
    detector_debug: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "bbox": [round(float(v), 2) for v in self.bbox],
            "bbox_space": self.bbox_space,
            "page": self.page,
            "source": self.source,
            "region_type_guess": self.region_type_guess,
            "width": self.width,
            "height": self.height,
            "format": self.image_format,
            "image_base64": base64.b64encode(self.image_bytes).decode("ascii"),
        }


# =========================================================================
# Path 1: native-PDF embedded images
# =========================================================================

# pdfplumber image stream /Filter -> (pillow-loadable?, extension). For the
# common photographic filters we can hand the raw stream bytes straight
# back. Anything else we re-render from a page crop (see _render_crop).
_DIRECT_FILTERS = {
    "DCTDecode": "jpg",
    "DCTDecode1": "jpg",
    "JPXDecode": "jp2",
}


def _filter_name(stream) -> str | None:
    f = None
    try:
        f = stream.attrs.get("Filter")
    except Exception:  # noqa: BLE001
        f = stream.get("Filter") if hasattr(stream, "get") else None
    if f is None:
        return None
    if isinstance(f, (list, tuple)):
        f = f[-1] if f else None
    name = getattr(f, "name", None) or str(f)
    return name.lstrip("/")


def extract_native_pdf_images(pdf) -> list[ExtractedImage]:
    """`pdf` is an already-open pdfplumber.PDF. Returns one ExtractedImage
    per DISTINCT embedded raster image across all pages. Deduplicated both
    by (page, rounded bbox) and by exact byte content - the same letterhead
    logo / QR repeated on every page collapses to one entry (recorded
    against the first page it appears on)."""
    import hashlib

    out: list[ExtractedImage] = []
    seen: set[tuple] = set()
    seen_hashes: set[str] = set()
    for pageno, page in enumerate(pdf.pages):
        for im in page.images:
            try:
                x0 = float(im["x0"])
                x1 = float(im["x1"])
                top = float(im["top"])
                bottom = float(im["bottom"])
            except (KeyError, TypeError, ValueError):
                continue
            key = (pageno, round(x0), round(top), round(x1), round(bottom))
            if key in seen:
                continue
            seen.add(key)

            raw, fmt = _native_image_bytes(page, im)
            if raw is None:
                continue
            digest = hashlib.md5(raw).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            try:
                pil = Image.open(io.BytesIO(raw))
                pil.load()
                w, h = pil.size
            except Exception as exc:  # noqa: BLE001 - keep going, skip this one
                logger.warning("page %d: embedded image failed to decode: %s", pageno, exc)
                continue

            out.append(
                ExtractedImage(
                    bbox=[x0, top, x1, bottom],
                    image_bytes=raw,
                    region_type_guess=_classify_pil(pil, (x0, top, x1, bottom), page.width, page.height),
                    source="embedded",
                    bbox_space="pdf_points",
                    page=pageno,
                    width=w,
                    height=h,
                    image_format=fmt,
                )
            )
            if len(out) >= _MAX_REGIONS:
                return out
    return out


def _native_image_bytes(page, im) -> tuple[bytes | None, str]:
    """Prefer the raw embedded stream (original pixels, no re-encoding);
    fall back to rendering the page region if the filter isn't one we can
    hand back directly."""
    stream = im.get("stream")
    if stream is not None:
        filt = _filter_name(stream)
        if filt in _DIRECT_FILTERS:
            try:
                data = stream.get_data()
                if data:
                    return data, _DIRECT_FILTERS[filt]
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream.get_data() failed (%s); rendering instead", exc)
    return _render_crop(page, im), "png"


def _render_crop(page, im) -> bytes | None:
    try:
        bbox = (float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"]))
        # clamp to the page box - pdfplumber raises on out-of-bounds crops
        bbox = (
            max(0, bbox[0]), max(0, bbox[1]),
            min(page.width, bbox[2]), min(page.height, bbox[3]),
        )
        if bbox[2] - bbox[0] < 1 or bbox[3] - bbox[1] < 1:
            return None
        pimg = page.crop(bbox).to_image(resolution=200)
        buf = io.BytesIO()
        pimg.original.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("page-crop render failed: %s", exc)
        return None


# =========================================================================
# Path 2: detection in a rasterised / scanned page
# =========================================================================
#
# What actually separated pictures from text/tables on the real samples
# this was tuned against (scanned & digital DEXA reports, Gulf-employment
# medical forms, a text-only lab report):
#
#   * a PICTURE region is one large connected mass of non-text content -
#     either strongly coloured (`hi_sat_frac`), a dark continuous-tone scan
#     (`black_frac`), or a pale continuous-tone scan (lots of *soft*
#     darkening vs local background, little *hard* text-stroke darkening).
#   * a TEXT block is thousands of tiny disconnected marks - high
#     `glyph_cover`, no large solid blob.
#   * a RULED TABLE is long thin lines - high `ruled`, low blob, low colour.
#
# Honest limitations (measured, see README): misses small logos, faded
# photos and ruled-border stamps; over-segments a scan crossed by vector
# overlay lines; the type label is a guess. It does NOT false-positive on
# body text or tables in the samples tested, which is the property that
# matters most here (a text block wrongly returned as an "image" would be
# dropped from OCR downstream).

_WORK_MAX = 2200
_MIN_AREA_FRAC = 0.0030
_MAX_AREA_FRAC = 0.60
_MIN_DIM = 34


def _resize_for_work(img: np.ndarray) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    s = _WORK_MAX / max(h, w) if max(h, w) > _WORK_MAX else 1.0
    if s != 1.0:
        img = cv2.resize(img, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
    return img, s


def _glyph_mask(gray: np.ndarray) -> np.ndarray:
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 12
    )
    n, lab, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    gm = np.zeros(gray.shape, np.uint8)
    h_img, w_img = gray.shape
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if 4 <= h <= 48 and 2 <= w <= 95 and 6 <= a <= 2800 and w < 0.5 * w_img and h < 0.09 * h_img:
            if 0.08 <= a / (w * h + 1e-6) <= 0.98:
                gm[lab == i] = 255
    return gm


def _bg_darker(gray: np.ndarray) -> np.ndarray:
    """How much darker each pixel is than its local background (median
    blur). Kills page tint, zebra row striping and pale table fills."""
    k = max(31, (min(gray.shape) // 20) | 1)
    k = min(k, 99)
    return cv2.subtract(cv2.medianBlur(gray, k), gray)


def _colorfulness(bgr: np.ndarray) -> float:
    b, g, r = cv2.split(bgr.astype(np.float32))
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.var() + yb.var()) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def _ruled_score(gray_roi: np.ndarray) -> float:
    if gray_roi.size == 0:
        return 0.0
    e = cv2.Canny(gray_roi, 50, 150)
    h, w = e.shape
    hz = cv2.morphologyEx(e, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, w // 3), 1)))
    vt = cv2.morphologyEx(e, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, h // 3))))
    return float(max((hz > 0).sum(), (vt > 0).sum()) / (h * w + 1e-6)) * 20.0


def _blob_shape(darker_roi: np.ndarray, gm_roi: np.ndarray) -> tuple[float, float, float, int]:
    m = cv2.bitwise_and((darker_roi > 16).astype(np.uint8) * 255, cv2.bitwise_not(gm_roi))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return 0.0, 0.0, 0.0, 0
    i = max(range(1, n), key=lambda k: stats[k][4])
    x, y, w, h, a = stats[i]
    cnts, _ = cv2.findContours((lab == i).astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    sol = 0.0
    if cnts:
        c = max(cnts, key=cv2.contourArea)
        hull = cv2.contourArea(cv2.convexHull(c))
        sol = cv2.contourArea(c) / (hull + 1e-6)
    return a / (darker_roi.size + 1e-6), sol, a / (w * h + 1e-6), int(min(w, h))


def _region_stats(work, gray, darker, sat, gm, x, y, w, h, comp) -> dict:
    gr = gray[y:y + h, x:x + w]
    st = sat[y:y + h, x:x + w]
    gmr = gm[y:y + h, x:x + w]
    dk = darker[y:y + h, x:x + w]
    roi = work[y:y + h, x:x + w]
    blob, blob_sol, blob_ext, blob_min = _blob_shape(dk, gmr)
    return {
        "area_frac": w * h / (work.shape[0] * work.shape[1]),
        "glyph_cover": float((gmr > 0).mean()),
        "white_frac": float((gr > 245).mean()),
        "black_frac": float((gr < 45).mean()),
        "soft_frac": float(((dk > 4) & (dk < 60)).mean()),
        "hard_frac": float((dk >= 60).mean()),
        "mean_sat": float(st[comp].mean()) if comp.any() else 0.0,
        "hi_sat_frac": float((st > 90).mean()),
        "std": float(gr.std()),
        "colorfulness": _colorfulness(roi),
        "ruled": _ruled_score(gr),
        "blob": blob,
        "blob_sol": blob_sol,
        "blob_ext": blob_ext,
        "blob_min": blob_min,
        "fill": float(comp.mean()),
        "aspect": w / (h + 1e-6),
        "cy": (y + h / 2) / work.shape[0],
    }


def _is_pictorial(s: dict) -> bool:
    if not (_MIN_AREA_FRAC <= s["area_frac"] <= _MAX_AREA_FRAC):
        return False
    if s["fill"] < 0.32 and s["area_frac"] > 0.14:
        return False  # sprawling merge artefact
    if s["aspect"] > 3.5 and s["blob_sol"] < 0.45:
        return False  # wide banner / merged headings
    if s["ruled"] > 0.05 and s["blob"] < 0.25 and s["colorfulness"] < 22:
        return False  # ruled table / axis strip
    if s["glyph_cover"] > 0.20 and s["blob_sol"] < 0.5:
        return False  # text block
    coloured = s["colorfulness"] > 24 and s["hi_sat_frac"] > 0.08
    dark_scan = s["black_frac"] > 0.10 and s["white_frac"] < 0.75
    tone = (
        s["blob"] > 0.18 and s["blob_sol"] > 0.42 and s["blob_ext"] > 0.42
        and s["blob_min"] > 55 and s["std"] > 12 and s["ruled"] < 0.15
    )
    pale = (
        s["soft_frac"] > 0.09 and s["hard_frac"] < 0.6 * s["soft_frac"]
        and s["glyph_cover"] < 0.10 and s["blob_min"] > 55 and s["area_frac"] > 0.010
        and s["ruled"] < 0.15
    )
    return bool(coloured or dark_scan or tone or pale)


def _classify_stats(s: dict) -> str:
    if s["colorfulness"] > 28 and s["hi_sat_frac"] > 0.08:
        return "chart" if (s["ruled"] > 0.005 or s["white_frac"] > 0.12) else "photo"
    if s["black_frac"] > 0.10 and s["mean_sat"] < 45:
        return "photo"
    if s["cy"] < 0.16 and s["aspect"] > 2.3:
        return "logo"
    if s["ruled"] < 0.006 and 0.75 <= s["aspect"] <= 1.3 and s["area_frac"] < 0.03:
        return "stamp"
    if 0.5 <= s["aspect"] <= 1.1 and s["area_frac"] < 0.06:
        return "photo"
    if s["std"] > 11 and s["white_frac"] < 0.96:
        return "photo"
    return "unknown"


def detect_regions(bgr: np.ndarray) -> list[tuple[list[int], str, dict]]:
    """Return [(bbox_xyxy_in_original_pixels, region_type_guess, debug), ...]."""
    work, scale = _resize_for_work(bgr)
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    sat = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)[:, :, 1]
    darker = _bg_darker(gray)
    gm = _glyph_mask(gray)

    textmask = cv2.dilate(gm, cv2.getStructuringElement(cv2.MORPH_RECT, (13, 9)))
    content = (((darker > 16) | (sat > 70)).astype(np.uint8) * 255)
    pic = cv2.bitwise_and(content, cv2.bitwise_not(textmask))
    pic = cv2.morphologyEx(pic, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (19, 19)))
    pic = cv2.morphologyEx(pic, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))

    n, lab, stats, _ = cv2.connectedComponentsWithStats(pic, connectivity=8)
    found: list[tuple[list[int], str, dict, int]] = []
    for i in range(1, n):
        x, y, w, h, _a = stats[i]
        if w < _MIN_DIM or h < _MIN_DIM:
            continue
        comp = lab[y:y + h, x:x + w] == i
        s = _region_stats(work, gray, darker, sat, gm, x, y, w, h, comp)
        if not _is_pictorial(s):
            continue
        bbox = [int(x / scale), int(y / scale), int((x + w) / scale), int((y + h) / scale)]
        found.append((bbox, _classify_stats(s), {k: round(v, 3) for k, v in s.items()}, w * h))

    found.sort(key=lambda t: t[3], reverse=True)
    return [(b, g, d) for b, g, d, _ in found[:_MAX_REGIONS]]


# =========================================================================
# Shared: crop encoding + best-effort classification of an already-decoded image
# =========================================================================

def _encode_crop(pil: Image.Image, prefer_jpeg: bool) -> tuple[bytes, str, int, int]:
    im = pil
    if max(im.size) > _MAX_RETURN_DIM:
        r = _MAX_RETURN_DIM / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    buf = io.BytesIO()
    if prefer_jpeg:
        im.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
        return buf.getvalue(), "jpg", im.width, im.height
    im.save(buf, format="PNG")
    return buf.getvalue(), "png", im.width, im.height


def _classify_pil(pil: Image.Image, bbox, page_w, page_h) -> str:
    """Cheap best-effort type guess for an already-decoded (native) image."""
    arr = np.array(pil.convert("RGB"))
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    sat = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[:, :, 1]
    x0, top, x1, bottom = bbox
    s = {
        "colorfulness": _colorfulness(bgr),
        "hi_sat_frac": float((sat > 90).mean()),
        "black_frac": float((gray < 45).mean()),
        "white_frac": float((gray > 245).mean()),
        "mean_sat": float(sat.mean()),
        "std": float(gray.std()),
        "ruled": _ruled_score(gray),
        "aspect": (x1 - x0) / (bottom - top + 1e-6),
        "cy": ((top + bottom) / 2) / (page_h + 1e-6),
        "area_frac": ((x1 - x0) * (bottom - top)) / (page_w * page_h + 1e-6),
    }
    # QR / barcode: near-square, high-contrast, ~no colour, bimodal
    if 0.7 <= s["aspect"] <= 1.4 and s["mean_sat"] < 25 and s["black_frac"] > 0.15 and s["white_frac"] > 0.15:
        return "unknown"  # honestly not one of our labels
    return _classify_stats(s)


# =========================================================================
# Entry points used by app/ocr.py
# =========================================================================

def images_from_page_array(bgr: np.ndarray, page: int = 0) -> list[ExtractedImage]:
    """Detect + crop pictorial regions from one rasterised page (BGR)."""
    out: list[ExtractedImage] = []
    for bbox, guess, dbg in detect_regions(bgr):
        x0, y0, x1, y1 = bbox
        if x1 - x0 < _MIN_REGION_PX or y1 - y0 < _MIN_REGION_PX:
            continue
        crop = bgr[max(0, y0):y1, max(0, x0):x1]
        if crop.size == 0:
            continue
        pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        prefer_jpeg = guess in ("photo", "chart") or max(pil.size) > _MAX_RETURN_DIM
        data, fmt, w, h = _encode_crop(pil, prefer_jpeg)
        out.append(
            ExtractedImage(
                bbox=[x0, y0, x1, y1],
                image_bytes=data,
                region_type_guess=guess,
                source="detected",
                bbox_space="page_pixels",
                page=page,
                width=w,
                height=h,
                image_format=fmt,
                detector_debug=dbg,
            )
        )
    return out
