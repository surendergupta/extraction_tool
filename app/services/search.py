"""Postgres full-text search over Document.extracted_text."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document

_TS_CONFIG = "english"


async def search_documents(
    db: AsyncSession, query: str, limit: int = 20, offset: int = 0
) -> list[tuple[Document, float, str]]:
    """Full-text search documents by `query`.

    Returns a list of (Document, rank, snippet) tuples ordered by rank desc.
    Uses `plainto_tsquery` so callers can pass plain natural-language text
    rather than tsquery syntax.
    """
    ts_query = func.plainto_tsquery(_TS_CONFIG, query)
    rank = func.ts_rank(Document.search_vector, ts_query).label("rank")
    snippet = func.ts_headline(
        _TS_CONFIG,
        func.coalesce(Document.extracted_text, ""),
        ts_query,
        "MaxFragments=1, MaxWords=35, MinWords=15",
    ).label("snippet")

    stmt = (
        select(Document, rank, snippet)
        .where(Document.search_vector.op("@@")(ts_query))
        .order_by(rank.desc(), Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return [(row.Document, row.rank, row.snippet) for row in result]
