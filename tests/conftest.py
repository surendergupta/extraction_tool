import io
import os
import tempfile
import uuid

# --- Test environment must be set before any `app.*` module is imported ---
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sense_tool:sense_tool@localhost:5432/sense_tool_test"
)
os.environ["DATABASE_URL_SYNC"] = os.environ.get(
    "TEST_DATABASE_URL_SYNC", "postgresql+psycopg2://sense_tool:sense_tool@localhost:5432/sense_tool_test"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
_TEST_STORAGE_DIR = tempfile.mkdtemp(prefix="sense_tool_test_storage_")
os.environ["STORAGE_DIR"] = _TEST_STORAGE_DIR

import psycopg2  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402


def _ensure_test_database() -> None:
    conn = psycopg2.connect(
        host="localhost", port=5432, user="sense_tool", password="sense_tool", dbname="postgres"
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", ("sense_tool_test",))
            if not cur.fetchone():
                cur.execute("CREATE DATABASE sense_tool_test")
    finally:
        conn.close()


_ensure_test_database()

from app import models  # noqa: E402,F401  registers Document on Base.metadata
from app.db import AsyncSessionLocal, Base, engine  # noqa: E402
from app.enums import DocumentStatus  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Document  # noqa: E402
from app.storage import get_storage_backend  # noqa: E402


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _prepare_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    yield
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE documents RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture(autouse=True)
async def _clean_redis():
    """Flush the test Redis db before each test so leftover queued/in-flight
    arq jobs from a previous test never leak into the next one."""
    from app.queue import get_arq_pool

    pool = await get_arq_pool()
    await pool.flushdb()
    yield


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def png_bytes() -> bytes:
    img = Image.new("RGB", (200, 60), color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest_asyncio.fixture
async def make_document(db_session: AsyncSession):
    """Factory: create+persist a Document row directly (bypasses intake/queue)."""

    async def _make(
        *,
        source: str = "Test Clinic",
        status: DocumentStatus = DocumentStatus.QUEUED,
        raw_file_path: str | None = None,
        extracted_text: str | None = None,
        structured_data: dict | None = None,
        image_regions: list | None = None,
        error_message: str | None = None,
    ) -> Document:
        document_id = uuid.uuid4()
        if raw_file_path is None:
            storage = get_storage_backend()
            key = storage.build_key(document_id, "sample.png")
            storage.save(key, b"fake-bytes-not-a-real-image")
            raw_file_path = key
        document = Document(
            id=document_id,
            source=source,
            status=status,
            raw_file_path=raw_file_path,
            extracted_text=extracted_text,
            structured_data=structured_data,
            image_regions=image_regions,
            error_message=error_message,
        )
        db_session.add(document)
        await db_session.commit()
        await db_session.refresh(document)
        return document

    return _make


class FakeCtx(dict):
    """Minimal stand-in for the Arq job `ctx` dict used by worker tasks."""


@pytest_asyncio.fixture
async def worker_ctx():
    """A ctx dict wired to the test DB/session pool + a real (test-db) Redis pool."""
    from app.queue import get_arq_pool

    ctx = FakeCtx()
    ctx["sessionmaker"] = AsyncSessionLocal
    ctx["redis"] = await get_arq_pool()
    yield ctx
