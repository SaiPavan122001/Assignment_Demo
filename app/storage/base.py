from abc import ABC, abstractmethod


class Storage(ABC):
    """Blob storage. POC: local folder. Production counterpart: an object store."""

    @abstractmethod
    def put(self, data: bytes, suffix: str = "") -> str:
        """Store bytes, return an opaque key."""

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def local_path(self, key: str) -> str:
        """Filesystem path for adapters that need one (zip, pdf readers)."""
