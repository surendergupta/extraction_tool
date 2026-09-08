import os
import uuid
from pathlib import Path

from app.storage.base import StorageBackend


class LocalFilesystemStorage(StorageBackend):
    """Stores objects as files under a root directory on local disk."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _full_path(self, locator: str) -> Path:
        full = (self.root / locator).resolve()
        if self.root.resolve() not in full.parents and full != self.root.resolve():
            raise ValueError(f"Invalid storage locator: {locator!r}")
        return full

    def build_key(self, document_id: uuid.UUID, filename: str) -> str:
        safe_name = os.path.basename(filename or "upload")
        return f"{document_id}/{safe_name}"

    def save(self, key: str, data: bytes) -> str:
        path = self._full_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def read(self, locator: str) -> bytes:
        return self._full_path(locator).read_bytes()

    def resolve_local_path(self, locator: str) -> str:
        return str(self._full_path(locator))

    def exists(self, locator: str) -> bool:
        return self._full_path(locator).exists()
