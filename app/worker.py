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
from arq import func
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.enums import DocumentStatus
from app.models import Document
from app.services import ocr, structure
from app.storage import get_storage_backend

logger = logging.getLogger("sense_tool.worker")

settings = get_settings()

# The Tesseract language string used for OCR. English-only for now; WP-A's
# opt-in multi-script modes ("eng+guj", "eng+nep") are plumbed end to end
# but nothing wires a per-document choice yet. `run_ocr_extraction` takes it
# as an argument so that wiring is a one-line change later, and it is
# persisted to `Document.ocr_lang` so a searchable PDF (WP-G) can be
# regenerated with a matching text layer.
_DEFAULT_OCR_LANG = "eng"


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


def _persist_searchable_pdf(storage, document_id, pdf: bytes) -> str:
    """WP-G: save the Tesseract searchable PDF next to the raw file and
    return its storage key."""
    key = f"{document_id}/searchable.pdf"
    storage.save(key, pdf)
    logger.info("document %s: stored searchable PDF (%d bytes)", document_id, len(pdf))
    return key


async def run_ocr_extraction(
    ctx: dict, document_id: str, lang: str = _DEFAULT_OCR_LANG
) -> None:
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
            result = await ocr.extract_text(filename, data, lang=lang)

            document.extracted_text = result.text
            # WP-G: record which path produced the text and the language
            # used, so /export can pick the right PDF strategy (native ->
            # pass through the original file; ocr -> the searchable PDF) and
            # so the searchable-PDF job can match its text layer's language.
            document.text_source = result.text_source
            document.ocr_lang = lang
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

    # WP-G: the searchable PDF (for pixel-perfect PDF export of scanned
    # documents) is built by a SEPARATE follow-up job, on its own Tesseract
    # pass - decoupled from text extraction so it can never block or corrupt
    # `extracted_text`. Only the rasterize+OCR path needs one; a native-PDF
    # document exports as a pass-through of its own file.
    if result.text_source == "ocr":
        await redis.enqueue_job(
            "run_searchable_pdf_generation", str(document_id), lang
        )


async def run_searchable_pdf_generation(
    ctx: dict, document_id: str, lang: str = _DEFAULT_OCR_LANG
) -> None:
    """WP-G follow-up job: build the Tesseract searchable PDF for a scanned
    document and record it at `Document.searchable_pdf_key`.

    Entirely best-effort and isolated: any failure is logged and swallowed
    (no retry, no status change). `extracted_text`, `structured_data`,
    `status` and export availability are unaffected - the /export endpoint
    already falls back to the structured-text PDF when `searchable_pdf_key`
    is NULL.
    """
    sessionmaker = ctx["sessionmaker"]
    try:
        async with sessionmaker() as session:
            document = await session.get(Document, uuid.UUID(str(document_id)))
            if document is None:
                logger.warning("document %s not found for searchable-PDF step", document_id)
                return
            if not document.raw_file_path:
                logger.warning("document %s has no raw file for searchable-PDF step", document_id)
                return

            storage = get_storage_backend()
            data = storage.read(document.raw_file_path)
            filename = Path(document.raw_file_path).name
            pdf = await ocr.generate_searchable_pdf(filename, data, lang=lang)

            document.searchable_pdf_key = _persist_searchable_pdf(storage, document.id, pdf)
            await session.commit()
            logger.info("document %s: searchable PDF ready", document_id)
    except Exception as exc:  # noqa: BLE001 - best-effort; never affect the main pipeline
        logger.warning(
            "document %s: searchable-PDF generation failed (leaving key NULL, "
            "export falls back to the structured-text PDF): %s",
            document_id,
            exc,
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
    functions = [
        run_ocr_extraction,
        run_structure_parsing,
        # WP-G: its own generous job timeout - it runs a fresh Tesseract
        # pass per page (~21s/page worst case measured), well past arq's
        # 300s default for a large multi-page scan. It is isolated and
        # best-effort, so hitting this ceiling just leaves searchable_pdf_key
        # NULL (export falls back), it does not fail the document.
        func(run_searchable_pdf_generation, timeout=960),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
