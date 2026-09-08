import pytest
from app.enums import DocumentStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_search_finds_document_by_extracted_text(client, make_document):
    await make_document(
        source="Clinic A",
        status=DocumentStatus.DONE,
        extracted_text="Patient presents with acute bronchitis and mild fever.",
        structured_data={"sections": [], "tables": []},
    )
    await make_document(
        source="Clinic B",
        status=DocumentStatus.DONE,
        extracted_text="Routine annual checkup, no abnormal findings.",
        structured_data={"sections": [], "tables": []},
    )

    response = await client.get("/documents/search", params={"q": "bronchitis"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["source"] == "Clinic A"
    assert body["results"][0]["rank"] is not None


async def test_search_returns_empty_for_no_match(client, make_document):
    await make_document(
        status=DocumentStatus.DONE,
        extracted_text="Nothing relevant here.",
        structured_data={"sections": [], "tables": []},
    )

    response = await client.get("/documents/search", params={"q": "zzz_nonexistent_term"})
    assert response.status_code == 200
    assert response.json()["count"] == 0


async def test_search_ignores_documents_with_no_extracted_text(client, make_document):
    await make_document(status=DocumentStatus.QUEUED, extracted_text=None)

    response = await client.get("/documents/search", params={"q": "anything"})
    assert response.status_code == 200
    assert response.json()["count"] == 0


async def test_search_requires_query_param(client):
    response = await client.get("/documents/search")
    assert response.status_code == 422
