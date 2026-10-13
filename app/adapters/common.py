"""Shared helpers: ignore lists and a capped text-tree reader."""
import os

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".idea", ".vscode",
    "target", ".next", ".pytest_cache", ".mypy_cache", "vendor", ".gradle", "coverage", ".tox",
}
IGNORE_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", ".DS_Store"}

MAX_FILES = 500
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 5 * 1024 * 1024


def read_tree(root: str, max_files: int = MAX_FILES, max_file_bytes: int = MAX_FILE_BYTES,
              max_total_bytes: int = MAX_TOTAL_BYTES) -> tuple[dict[str, str], list[str]]:
    """Read text files under root. Returns (files, notes). Binary files and ignored dirs are skipped."""
    files: dict[str, str] = {}
    notes: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".git"))
        for fn in sorted(filenames):
            if fn in IGNORE_FILES:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                continue
            if len(files) >= max_files:
                notes.append(f"file cap {max_files} reached; remaining files ignored")
                return files, notes
            if total >= max_total_bytes:
                notes.append("total size cap reached; remaining files ignored")
                return files, notes
            try:
                with open(full, "rb") as f:
                    raw = f.read(max_file_bytes + 1)
            except OSError:
                continue
            if b"\x00" in raw[:8000]:
                continue  # binary
            truncated = len(raw) > max_file_bytes
            try:
                text = raw[:max_file_bytes].decode("utf-8")
            except UnicodeDecodeError:
                text = raw[:max_file_bytes].decode("utf-8", errors="replace")
            if truncated:
                notes.append(f"{rel} truncated at {max_file_bytes} bytes")
            files[rel] = text
            total += len(text)
    return files, notes
