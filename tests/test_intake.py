import uuid

import pytest

from app.enums import DocumentStatus
from app.models import Document
from app.storage import get_storage_backend

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_intake_saves_file_creates_queued_document_and_enqueues_job(
    client, db_session, png_bytes, worker_ctx
):
    response = await client.post(
        "/documents/intake",
        data={"source": "General Hospital"},
        files={"file": ("scan.png", png_bytes, "image/png")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["source"] == "General Hospital"
    document_id = uuid.UUID(body["id"])

    document = await db_session.get(Document, document_id)
    assert document is not None
    assert document.status == DocumentStatus.QUEUED
    assert document.extracted_text is None
    assert document.structured_data is None
    assert document.error_message is None

    storage = get_storage_backend()
    assert storage.exists(document.raw_file_path)
    assert storage.read(document.raw_file_path) == png_bytes

    queued = await worker_ctx["redis"].queued_jobs()
    job_functions = [j.function for j in queued]
    assert "run_ocr_extraction" in job_functions


async def test_intake_rejects_empty_file(client):
    response = await client.post(
        "/documents/intake",
        data={"source": "General Hospital"},
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert response.status_code == 400


async def test_intake_rejects_oversized_file(client, monkeypatch, png_bytes):
    import app.routers.documents as documents_module

    settings = documents_module.get_settings()
    monkeypatch.setattr(settings, "max_upload_size_bytes", 10)

    response = await client.post(
        "/documents/intake",
        data={"source": "General Hospital"},
        files={"file": ("scan.png", png_bytes, "image/png")},
    )
    assert response.status_code == 413


async def test_intake_requires_source_and_file(client):
    response = await client.post("/documents/intake", data={}, files={})
    assert response.status_code == 422


async def test_get_document_returns_full_record(client, make_document):
    document = await make_document(
        status=DocumentStatus.DONE,
        extracted_text="hello world",
        structured_data={"sections": [], "tables": []},
    )

    response = await client.get(f"/documents/{document.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(document.id)
    assert body["status"] == "DONE"
    assert body["extracted_text"] == "hello world"
    assert body["structured_data"] == {"sections": [], "tables": []}


async def test_get_document_404_when_missing(client):
    response = await client.get(f"/documents/{uuid.uuid4()}")
    assert response.status_code == 404
