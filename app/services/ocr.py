"""Client for the standalone OCR service (see ocr_service/).

OCR runs out-of-process on purpose: it's the one step in this pipeline with
heavy, easily-swappable dependencies (Tesseract today, maybe an additional
engine later), so it's isolated behind an HTTP call rather than living
in-process in the Arq worker.
"""

from dataclasses import dataclass, field

import httpx

from app.config import get_settings

# Raw pdfplumber table shape as returned by the OCR service: a table is a
# list of rows, each row a list of nullable cell strings. Only ever
# non-empty for documents that took the native-PDF-text path.
NativeTable = list[list[str | None]]


@dataclass
class OcrResult:
    text: str
    tables: list[NativeTable] = field(default_factory=list)


class OCRError(RuntimeError):
    """Raised when the OCR service fails or is unreachable."""


def _timeout(settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.ocr_connect_timeout_seconds,
        read=settings.ocr_read_timeout_seconds,
        write=settings.ocr_write_timeout_seconds,
        pool=settings.ocr_pool_timeout_seconds,
    )


async def extract_text(filename: str, data: bytes) -> OcrResult:
    settings = get_settings()

    async with httpx.AsyncClient(timeout=_timeout(settings)) as client:
        try:
            response = await client.post(
                f"{settings.ocr_service_url}/ocr",
                files={"file": (filename, data)},
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
    return OcrResult(text=payload["text"], tables=payload.get("tables", []))
