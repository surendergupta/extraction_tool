"""Application configuration, loaded from environment variables / .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Database ---
    # Async URL used by the FastAPI app and the Arq worker.
    database_url: str = "postgresql+asyncpg://sense_tool:sense_tool@localhost:5432/sense_tool"
    # Sync URL used by Alembic migrations.
    database_url_sync: str = "postgresql+psycopg2://sense_tool:sense_tool@localhost:5432/sense_tool"

    # --- Redis / Arq ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Storage ---
    # Directory used by the local filesystem storage backend.
    storage_dir: str = "./storage_data"

    # --- OCR ---
    # OCR runs as its own service (see ocr_service/); this app just calls it over HTTP.
    ocr_service_url: str = "http://localhost:8001"
    # Per-phase httpx timeouts for that call. ocr_read_timeout_seconds is the
    # one that matters for large documents (OCR itself, not connection setup)
    # - it's set from a real measurement, not a guess: a 25-page, 300 DPI
    # scanned PDF took ~102s against this service's Tesseract pipeline
    # (~4.1s/page), so 240s leaves real margin (~58 pages at that rate)
    # rather than the originally-proposed 90s, which that same real file
    # already exceeds. See ocr_service/README section and app/services/ocr.py.
    ocr_connect_timeout_seconds: float = 5.0
    ocr_read_timeout_seconds: float = 240.0
    ocr_write_timeout_seconds: float = 10.0
    ocr_pool_timeout_seconds: float = 5.0

    # --- Misc ---
    app_name: str = "Sense_tool"
    max_upload_size_bytes: int = 25 * 1024 * 1024  # 25 MB

    @property
    def storage_path(self) -> Path:
        path = Path(self.storage_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
