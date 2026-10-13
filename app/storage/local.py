import re
import uuid
from pathlib import Path

from app.config import get_settings
from app.storage.base import Storage


class LocalStorage(Storage):
    def __init__(self, root: str | None = None):
        self.root = Path(root or get_settings().upload_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, suffix: str = "") -> str:
        suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:10]  # never trust client filenames
        key = f"{uuid.uuid4().hex}{suffix}"
        (self.root / key).write_bytes(data)
        return key

    def local_path(self, key: str) -> str:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return str(p)

    def get(self, key: str) -> bytes:
        return Path(self.local_path(key)).read_bytes()


def get_storage() -> Storage:
    return LocalStorage()
