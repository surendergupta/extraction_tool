import functools

import pytesseract
from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel

from app.ocr import OCRError, extract_text_from_bytes
from app.searchable_pdf import SearchablePdfError, build_searchable_pdf

app = FastAPI(title="Sense_tool OCR Service")


class ImageRegionOut(BaseModel):
    """One extracted image/photo/chart region (WP-B). `region_type_guess` is
    a deliberately low-confidence best-effort label, not a claim of
    accuracy. See ocr_service/app/images.py and README.md."""

    bbox: list[float]
    bbox_space: str            # "pdf_points" (embedded) | "page_pixels" (detected)
    page: int
    source: str                # "embedded" | "detected"
    region_type_guess: str     # photo | chart | logo | stamp | unknown
    width: int
    height: int
    format: str                # "jpg" | "png" | "jp2"
    image_base64: str          # the crop bytes, base64


class TableRegionOut(BaseModel):
    """WP-D: one ruled-line table *region* from the rasterize+OCR path.
    `region_text` is a whole-region OCR blob - there is deliberately NO
    row/column grid (WP-D showed grid reconstruction is unreliable and
    cell-by-cell OCR is a net accuracy loss). `source` is always
    "ruled_line_region"; a consumer must branch on it because the
    native-PDF `tables` entries are a different shape (a real grid). See
    ocr_service/app/tables.py and README.md."""

    bbox: list[int]            # [x0, y0, x1, y1] in page pixels
    page: int
    region_text: str
    source: str                # always "ruled_line_region"


class OCRResponse(BaseModel):
    text: str
    # NATIVE-PDF path only. Raw pdfplumber shape: list of tables, each a
    # list of rows, each row a list of nullable cell strings - a real grid.
    # Always [] on the rasterize+OCR path. Sense_tool's structure-parsing
    # step decides how to fold it into structured_data.
    tables: list[list[list[str | None]]] = []
    # RASTERIZE+OCR path only (WP-D). Ruled-line table regions: bbox +
    # whole-region OCR text, NO grid. Always [] on the native-PDF path.
    # `tables` and `table_regions` are intentionally different shapes -
    # downstream branches on `source`, never assumes uniformity.
    table_regions: list[TableRegionOut] = []
    # Which path produced `text`: "native_pdf" | "ocr". Lets the caller pick
    # the right table source and skip the legacy space-heuristic on OCR text.
    text_source: str = "ocr"
    # WP-B: image/photo/chart regions. "embedded" entries are pdfplumber's
    # page.images on the native path (original bytes, bbox in PDF points);
    # "detected" entries are heuristic region crops from the rasterize+OCR
    # path (bbox in page pixels). Additive - `text`/`tables` are unchanged
    # by this. Not wired into export (that's a later WP).
    images: list[ImageRegionOut] = []


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# The `lang` value ends up on the Tesseract command line, so its shape is
# constrained to 3-letter ISO 639-2/T codes joined by "+" - nothing else can
# be smuggled through. Default "eng" keeps existing English-only callers
# byte-for-byte unchanged; combined modes ("eng+guj", "eng+nep") are
# strictly opt-in per request (see README.md for the measured English
# accuracy tradeoff that drove keeping them opt-in).
_LANG_PATTERN = r"^[a-z]{3}(\+[a-z]{3})*$"


@functools.lru_cache(maxsize=1)
def _installed_langs() -> frozenset[str]:
    """Language codes with tessdata actually baked into this image. Cached:
    the set is fixed for the life of the container (data is never fetched at
    runtime)."""
    try:
        return frozenset(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001 - never let this take the endpoint down
        return frozenset({"eng"})


def _validate_lang(lang: str) -> None:
    """Reject any requested sub-code whose pack isn't installed. Tesseract
    itself does NOT error on a partially-missing combined string - given
    `eng+zzz` with no zzz.traineddata it silently drops zzz and runs as
    `eng` (verified). That silent downgrade would be a confusing, invisible
    accuracy loss for a caller who asked for a script, so it's turned into
    an explicit 400 here instead."""
    installed = _installed_langs()
    missing = [code for code in lang.split("+") if code not in installed]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported OCR language(s): {'+'.join(missing)}. "
                f"Installed: {'+'.join(sorted(installed))}."
            ),
        )


@app.post("/ocr", response_model=OCRResponse)
async def ocr(
    file: UploadFile = File(...),
    psm: int = Query(6, ge=0, le=13, description="Tesseract page segmentation mode"),
    extract_images: bool = Query(
        True,
        description=(
            "WP-B: also return embedded/detected image/photo/chart regions in "
            "`images`. Text/table extraction is unaffected either way. Set false "
            "to skip the region detector (a little faster on large scans)."
        ),
    ),
    lang: str = Query(
        "eng",
        pattern=_LANG_PATTERN,
        description=(
            "Tesseract language(s): a 3-letter code or '+'-joined combination for "
            "single-pass multi-script OCR. Supported: eng, guj, nep (and combos, "
            "e.g. eng+guj, eng+nep). Only affects the rasterize+OCR path, not "
            "native-PDF-text extraction. Default eng."
        ),
    ),
) -> OCRResponse:
    _validate_lang(lang)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    try:
        result = extract_text_from_bytes(
            file.filename or "upload",
            data,
            psm=psm,
            lang=lang,
            extract_images=extract_images,
        )
    except OCRError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return OCRResponse(
        text=result.text,
        tables=result.tables,
        table_regions=[tr.to_payload() for tr in result.table_regions],
        text_source=result.text_source,
        images=[img.to_payload() for img in result.images],
    )


@app.post(
    "/searchable-pdf",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def searchable_pdf(
    file: UploadFile = File(...),
    lang: str = Query(
        "eng",
        pattern=_LANG_PATTERN,
        description=(
            "Tesseract language(s) for the invisible text layer - same syntax "
            "and support as /ocr's `lang`. Thread the same value the document "
            "was OCR'd with so the layer matches."
        ),
    ),
) -> Response:
    """WP-G: return a Tesseract *searchable PDF* - the ORIGINAL uploaded
    image / scanned page(s) with an invisible, word-positioned OCR text
    layer. The visible layer is a byte-faithful copy of the upload.

    This is a standalone job on purpose: the main app calls it from a
    follow-up Arq task *after* text extraction, so a failure here can never
    affect `Document.extracted_text`. It runs its own Tesseract pass
    (`--oem 1 --psm 6`) on the un-preprocessed image - see
    app/searchable_pdf.py for why the two passes are separate.
    """
    _validate_lang(lang)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    try:
        pdf = build_searchable_pdf(file.filename or "upload", data, lang)
    except SearchablePdfError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return Response(content=pdf, media_type="application/pdf")
