from app.storage.base import StorageBackend
from app.storage.local import LocalFilesystemStorage

__all__ = ["StorageBackend", "LocalFilesystemStorage", "get_storage_backend"]

_backend: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    """Return the process-wide storage backend instance.

    Swap this factory to return an S3-compatible backend later without
    touching any caller of the StorageBackend interface.
    """
    global _backend
    if _backend is None:
        from app.config import get_settings

        _backend = LocalFilesystemStorage(get_settings().storage_path)
    return _backend
