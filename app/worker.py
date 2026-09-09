"""Arq worker: OCR extraction, then structure parsing, per document.

Two chained jobs model the two pipeline steps from the spec:
  1. run_ocr_extraction  - QUEUED -> PROCESSING, calls the OCR service, sets
     extracted_text, then enqueues step 2.
  2. run_structure_parsing - parses sections/tables from extracted_text,
     sets structured_data, -> DONE.

Any failure in either step sets status=FAILED with error_message so a
document never sits silently stuck in PROCESSING.

OCR itself runs out-of-process (see app/services/ocr.py and ocr_service/):
this worker reads the raw bytes from storage and hands them to the OCR
service over HTTP, rather than running Tesseract in-process.
"""

import logging
import uuid
from pathlib import Path

import httpx
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.enums import DocumentStatus
from app.models import Document
from app.services import ocr, structure
from app.storage import get_storage_backend

logger = logging.getLogger("sense_tool.worker")

settings = get_settings()


async def startup(ctx: dict) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
    ctx["engine"] = engine
    ctx["sessionmaker"] = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def shutdown(ctx: dict) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


async def _fail(session: AsyncSession, document: Document, message: str) -> None:
    document.status = DocumentStatus.FAILED
    document.error_message = message
    await session.commit()
    logger.error("document %s failed: %s", document.id, message)


def _persist_image_regions(storage, document_id, images: list) -> list[dict] | None:
    """Save each crop's bytes under `{document_id}/images/{n}.{ext}` and
    return a JSON-serialisable metadata list for `Document.image_regions`.
    Returns None when there are no images so the column stays NULL."""
    if not images:
        return None
    meta: list[dict] = []
    for n, img in enumerate(images):
        ext = (img.image_format or "png").lstrip(".")
        key = f"{document_id}/images/{n}.{ext}"
        storage.save(key, img.data)
        meta.append(
            {
                "storage_key": key,
                "bbox": img.bbox,
                "bbox_space": img.bbox_space,
                "page": img.page,
                "source": img.source,
                "region_type_guess": img.region_type_guess,
                "width": img.width,
                "height": img.height,
                "format": ext,
            }
        )
    logger.info("document %s: stored %d extracted image region(s)", document_id, len(meta))
    return meta


async def run_ocr_extraction(ctx: dict, document_id: str) -> None:
    sessionmaker = ctx["sessionmaker"]
    async with sessionmaker() as session:
        document = await session.get(Document, uuid.UUID(str(document_id)))
        if document is None:
            logger.warning("document %s not found for OCR step", document_id)
            return

        try:
            document.status = DocumentStatus.PROCESSING
            await session.commit()

            storage = get_storage_backend()
            data = storage.read(document.raw_file_path)
            filename = Path(document.raw_file_path).name
            result = await ocr.extract_text(filename, data)

            document.extracted_text = result.text
            # WP-B: persist extracted image/photo/chart regions. Crop bytes
            # go to the storage backend next to the raw file; only metadata
            # (incl. the storage key) lands in the DB. Best-effort - a
            # failure here must not fail the OCR step, since text is the
            # primary product and images are additive.
            try:
                document.image_regions = _persist_image_regions(
                    storage, document.id, result.images
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("document %s: storing image regions failed: %s", document.id, exc)
            await session.commit()
        except httpx.TimeoutException:
            timeout_seconds = settings.ocr_read_timeout_seconds
            await _fail(session, document, f"OCR service timed out after {timeout_seconds:g}s")
            return
        except Exception as exc:  # noqa: BLE001 - any failure -> FAILED, never stuck
            await _fail(session, document, f"OCR extraction failed: {exc}")
            return

    redis = ctx["redis"]
    # These ride along to the next job rather than a DB column - only needed
    # transiently between these two chained steps:
    #  - native_tables: pdfplumber extract_tables() grids (native-PDF path only)
    #  - ruled_line_regions: WP-D ruled-line table regions (OCR path only)
    #  - text_source: "native_pdf" | "ocr" - picks which of the above applies
    #    and whether the legacy space-heuristic may run on the text.
    await redis.enqueue_job(
        "run_structure_parsing",
        str(document_id),
        result.tables,
        [
            {"bbox": tr.bbox, "region_text": tr.region_text, "page": tr.page}
            for tr in result.table_regions
        ],
        result.text_source,
    )


async def run_structure_parsing(
    ctx: dict,
    document_id: str,
    native_tables: list | None = None,
    ruled_line_regions: list | None = None,
    text_source: str | None = None,
) -> None:
    sessionmaker = ctx["sessionmaker"]
    async with sessionmaker() as session:
        document = await session.get(Document, uuid.UUID(str(document_id)))
        if document is None:
            logger.warning("document %s not found for structure step", document_id)
            return

        try:
            structured_data = structure.parse_structure(
                document.extracted_text,
                native_tables=native_tables,
                ruled_line_regions=ruled_line_regions,
                # The legacy space-heuristic table detector is pure garbage
                # on OCR text (WP-C/WP-D) - only let it run for native-PDF
                # text, where its module docstring notes it "never fires".
                space_heuristic_tables=(text_source != "ocr"),
            )
            document.structured_data = structured_data
            document.status = DocumentStatus.DONE
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await _fail(session, document, f"Structure parsing failed: {exc}")


class WorkerSettings:
    functions = [run_ocr_extraction, run_structure_parsing]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
