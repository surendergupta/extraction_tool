import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.enums import DocumentStatus


class DocumentIntakeResponse(BaseModel):
    """Fast-ack response returned immediately by POST /documents/intake."""

    id: uuid.UUID
    status: DocumentStatus
    source: str


class DocumentOut(BaseModel):
    """Full document record returned by GET /documents/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    status: DocumentStatus
    raw_file_path: str
    extracted_text: str | None = None
    structured_data: dict[str, Any] | None = None
    # WP-B: metadata for extracted image/photo/chart regions (bbox, page,
    # best-effort type guess, and the storage key of the crop). Detection +
    # extraction only - not consumed by structure parsing or export yet.
    image_regions: list[dict[str, Any]] | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class DocumentSummary(BaseModel):
    """Lightweight record used in search results."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime
    rank: float | None = None
    snippet: str | None = None


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[DocumentSummary]
