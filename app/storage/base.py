"""Object storage interface.

Abstracted so the local filesystem implementation can be swapped for an
S3-compatible backend later without changing any caller.
"""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes) -> str:
        """Persist `data` under `key` and return a locator string.

        The returned locator is what gets stored as `Document.raw_file_path`
        and is later handed back to `read`/`open_path` unchanged.
        """

    @abstractmethod
    def read(self, locator: str) -> bytes:
        """Read back the full contents addressed by `locator`."""

    @abstractmethod
    def resolve_local_path(self, locator: str) -> str:
        """Return a local filesystem path usable by tools (e.g. Tesseract)
        that need a real file path rather than bytes in memory.

        For remote backends this may materialize a temporary local copy.
        """

    @abstractmethod
    def exists(self, locator: str) -> bool:
        """Return whether the object addressed by `locator` exists."""
