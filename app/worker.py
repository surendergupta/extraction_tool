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
            await session.commit()
        except httpx.TimeoutException:
            timeout_seconds = settings.ocr_read_timeout_seconds
            await _fail(session, document, f"OCR service timed out after {timeout_seconds:g}s")
            return
        except Exception as exc:  # noqa: BLE001 - any failure -> FAILED, never stuck
            await _fail(session, document, f"OCR extraction failed: {exc}")
            return

    redis = ctx["redis"]
    # native_tables (pdfplumber's extract_tables() output, from the
    # native-PDF-text path only - empty otherwise) rides along to the next
    # job rather than a DB column, since it's only needed transiently
    # between these two chained steps.
    await redis.enqueue_job("run_structure_parsing", str(document_id), result.tables)


async def run_structure_parsing(
    ctx: dict, document_id: str, native_tables: list | None = None
) -> None:
    sessionmaker = ctx["sessionmaker"]
    async with sessionmaker() as session:
        document = await session.get(Document, uuid.UUID(str(document_id)))
        if document is None:
            logger.warning("document %s not found for structure step", document_id)
            return

        try:
            structured_data = structure.parse_structure(
                document.extracted_text, native_tables=native_tables
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
