"""Client for the standalone OCR service (see ocr_service/).

OCR runs out-of-process on purpose: it's the one step in this pipeline with
heavy, easily-swappable dependencies (Tesseract today, maybe an additional
engine later), so it's isolated behind an HTTP call rather than living
in-process in the Arq worker.
"""

import base64
import binascii
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

# Raw pdfplumber table shape as returned by the OCR service: a table is a
# list of rows, each row a list of nullable cell strings. Only ever
# non-empty for documents that took the native-PDF-text path.
NativeTable = list[list[str | None]]


@dataclass
class OcrImage:
    """One image/photo/chart region returned by the OCR service (WP-B).

    `data` is the decoded crop bytes. `bbox`/`bbox_space`/`page` locate it in
    the source document; `region_type_guess` is a low-confidence best-effort
    label. Detection + extraction only - not wired into export yet.
    """

    data: bytes
    bbox: list[float]
    bbox_space: str
    page: int
    source: str
    region_type_guess: str
    width: int
    height: int
    image_format: str


@dataclass
class RuledTableRegion:
    """WP-D: a ruled-line table *region* from the OCR service's
    rasterize+OCR path. `region_text` is a whole-region OCR blob - there is
    deliberately NO row/column grid. Distinct in shape from `tables`
    entries (native-PDF grids); the structure parser branches on `source`.
    """

    bbox: list[int]
    region_text: str
    page: int = 0


@dataclass
class OcrResult:
    text: str
    # native-PDF path: real grids. rasterize+OCR path: always [].
    tables: list[NativeTable] = field(default_factory=list)
    # rasterize+OCR path (WP-D): ruled-line table regions (text blobs, no
    # grid). native-PDF path: always [].
    table_regions: list[RuledTableRegion] = field(default_factory=list)
    # "native_pdf" | "ocr" - which path produced `text`.
    text_source: str = "ocr"
    images: list[OcrImage] = field(default_factory=list)


class OCRError(RuntimeError):
    """Raised when the OCR service fails or is unreachable."""


def _timeout(settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.ocr_connect_timeout_seconds,
        read=settings.ocr_read_timeout_seconds,
        write=settings.ocr_write_timeout_seconds,
        pool=settings.ocr_pool_timeout_seconds,
    )


async def extract_text(filename: str, data: bytes, lang: str | None = None) -> OcrResult:
    """`lang` is passed straight through to the OCR service's `/ocr` `lang`
    query param (e.g. "eng+guj", "eng+nep") for opt-in multi-script OCR.
    When omitted, no `lang` param is sent and the service applies its own
    default ("eng") - existing English-only callers are unaffected.
    """
    settings = get_settings()
    params = {"lang": lang} if lang else None

    async with httpx.AsyncClient(timeout=_timeout(settings)) as client:
        try:
            response = await client.post(
                f"{settings.ocr_service_url}/ocr",
                files={"file": (filename, data)},
                params=params,
            )
        except httpx.TimeoutException:
            # Let this propagate as-is (not wrapped in OCRError) so
            # run_ocr_extraction can catch it specifically and report a
            # clear timeout message rather than a generic failure.
            raise
        except httpx.HTTPError as exc:
            raise OCRError(f"OCR service request failed: {exc}") from exc

    if response.status_code != 200:
        raise OCRError(f"OCR service returned {response.status_code}: {response.text}")

    payload = response.json()
    return OcrResult(
        text=payload["text"],
        tables=payload.get("tables", []),
        table_regions=_parse_table_regions(payload.get("table_regions", [])),
        text_source=payload.get("text_source", "ocr"),
        images=_parse_images(payload.get("images", [])),
    )


def _parse_table_regions(raw: list[dict]) -> list[RuledTableRegion]:
    regions: list[RuledTableRegion] = []
    for item in raw:
        text = item.get("region_text")
        if not isinstance(text, str) or not text.strip():
            continue
        regions.append(
            RuledTableRegion(
                bbox=item.get("bbox", []),
                region_text=text,
                page=item.get("page", 0),
            )
        )
    return regions


def _parse_images(raw: list[dict]) -> list[OcrImage]:
    """Decode the base64 crop payloads. A malformed entry is skipped, never
    fatal - image regions are additive and must not break the OCR step."""
    images: list[OcrImage] = []
    for item in raw:
        try:
            data = base64.b64decode(item["image_base64"], validate=True)
        except (KeyError, TypeError, binascii.Error):
            continue
        if not data:
            continue
        images.append(
            OcrImage(
                data=data,
                bbox=item.get("bbox", []),
                bbox_space=item.get("bbox_space", "page_pixels"),
                page=item.get("page", 0),
                source=item.get("source", "detected"),
                region_type_guess=item.get("region_type_guess", "unknown"),
                width=item.get("width", 0),
                height=item.get("height", 0),
                image_format=item.get("format", "png"),
            )
        )
    return images
