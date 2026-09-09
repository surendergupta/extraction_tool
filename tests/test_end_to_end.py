"""Full round trip through the real Arq queue: intake -> queue -> worker(s).

Unlike test_pipeline.py (which invokes the worker task functions directly),
this drives an actual arq.worker.Worker in burst mode against the real Redis
test instance, so the enqueue/dequeue path itself is exercised too.
"""

import asyncio
import uuid

import pytest
from arq.worker import Worker

from app.enums import DocumentStatus
from app.models import Document
from app.services.ocr import OcrResult
from app.worker import WorkerSettings

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _run_worker_burst() -> None:
    worker = Worker(
        functions=WorkerSettings.functions,
        redis_settings=WorkerSettings.redis_settings,
        on_startup=WorkerSettings.on_startup,
        on_shutdown=WorkerSettings.on_shutdown,
        burst=True,
        poll_delay=0.05,
    )
    try:
        await worker.async_run()
    finally:
        await worker.close()


async def test_intake_through_real_queue_reaches_done(
    client, db_session, png_bytes, monkeypatch
):
    async def fake_extract_text(filename: str, data: bytes, **kwargs) -> OcrResult:
        return OcrResult(text="IMPRESSION:\nNormal chest x-ray.")

    monkeypatch.setattr("app.worker.ocr.extract_text", fake_extract_text)

    response = await client.post(
        "/documents/intake",
        data={"source": "Riverside Clinic"},
        files={"file": ("scan.png", png_bytes, "image/png")},
    )
    assert response.status_code == 202
    document_id = uuid.UUID(response.json()["id"])

    await asyncio.wait_for(_run_worker_burst(), timeout=15)

    document = await db_session.get(Document, document_id)
    await db_session.refresh(document)
    assert document.status == DocumentStatus.DONE
    assert document.extracted_text == "IMPRESSION:\nNormal chest x-ray."
    assert document.structured_data["sections"][0]["title"] == "IMPRESSION"


async def test_intake_through_real_queue_marks_failed_on_ocr_error(
    client, db_session, png_bytes, monkeypatch
):
    async def boom(filename: str, data: bytes, **kwargs) -> str:
        raise RuntimeError("ocr backend unavailable")

    monkeypatch.setattr("app.worker.ocr.extract_text", boom)

    response = await client.post(
        "/documents/intake",
        data={"source": "Riverside Clinic"},
        files={"file": ("scan.png", png_bytes, "image/png")},
    )
    assert response.status_code == 202
    document_id = uuid.UUID(response.json()["id"])

    await asyncio.wait_for(_run_worker_burst(), timeout=15)

    document = await db_session.get(Document, document_id)
    await db_session.refresh(document)
    assert document.status == DocumentStatus.FAILED
    assert "ocr backend unavailable" in document.error_message
