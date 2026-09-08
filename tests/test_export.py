import pytest
import uuid

from app.enums import DocumentStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")

STRUCTURED = {
    "sections": [{"title": "History", "content": "No prior admissions."}],
    "tables": [{"raw_lines": ["WBC | 7.2"], "rows": [["WBC", "7.2"]]}],
}


async def test_export_pdf(client, make_document):
    document = await make_document(status=DocumentStatus.DONE, structured_data=STRUCTURED)
    response = await client.get(f"/documents/{document.id}/export", params={"format": "pdf"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


async def test_export_docx(client, make_document):
    document = await make_document(status=DocumentStatus.DONE, structured_data=STRUCTURED)
    response = await client.get(f"/documents/{document.id}/export", params={"format": "docx"})
    assert response.status_code == 200
    assert "wordprocessingml" in response.headers["content-type"]
    assert response.content.startswith(b"PK")


async def test_export_xlsx(client, make_document):
    document = await make_document(status=DocumentStatus.DONE, structured_data=STRUCTURED)
    response = await client.get(f"/documents/{document.id}/export", params={"format": "xlsx"})
    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    assert response.content.startswith(b"PK")


async def test_export_rejects_invalid_format(client, make_document):
    document = await make_document(status=DocumentStatus.DONE, structured_data=STRUCTURED)
    response = await client.get(f"/documents/{document.id}/export", params={"format": "txt"})
    assert response.status_code == 422


async def test_export_404_when_missing(client):
    response = await client.get(f"/documents/{uuid.uuid4()}/export", params={"format": "pdf"})
    assert response.status_code == 404


async def test_export_409_when_not_done(client, make_document):
    document = await make_document(status=DocumentStatus.QUEUED)
    response = await client.get(f"/documents/{document.id}/export", params={"format": "pdf"})
    assert response.status_code == 409


async def test_export_409_when_failed(client, make_document):
    document = await make_document(status=DocumentStatus.FAILED, error_message="boom")
    response = await client.get(f"/documents/{document.id}/export", params={"format": "pdf"})
    assert response.status_code == 409
