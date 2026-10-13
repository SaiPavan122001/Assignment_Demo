"""Safe ZIP extraction. Student code is only ever unpacked and read as text, never executed.

Rejects the whole archive on: zip-slip, absolute paths, symlinks, encryption, too many files,
and ZIP bombs (declared OR actually-streamed size/ratio over limits; declared sizes can lie).
Files outside the allowlist are skipped and reported; executables are skipped and flagged.
"""
import os
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

ALLOWED_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".cs",
    ".kt", ".swift", ".sql", ".sh", ".html", ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".xml", ".md", ".txt", ".rst", ".csv", ".ipynb", ".lock", ".gradle", ".dockerfile", ".env.example",
    ".pdf", ".docx", ".srt", ".vtt", ".png", ".jpg", ".jpeg", ".gif", ".svg",
}
ALLOWED_NAMES = {"dockerfile", "makefile", "license", "readme", "requirements.txt", ".gitignore"}
EXECUTABLE_EXT = {".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".msi", ".scr", ".com", ".jar", ".apk"}


class ZipRejected(Exception):
    pass


@dataclass
class ZipLimits:
    max_files: int = 1000
    max_total_bytes: int = 100 * 1024 * 1024
    max_member_bytes: int = 20 * 1024 * 1024
    max_ratio: int = 100  # total uncompressed / archive size


@dataclass
class ZipReport:
    extracted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # not on allowlist
    flagged_executables: list[str] = field(default_factory=list)


def _clean_name(raw: str) -> str:
    name = raw.replace("\\", "/")
    p = PurePosixPath(name)
    if p.is_absolute() or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ZipRejected(f"absolute path in archive: {raw!r}")
    if ".." in p.parts:
        raise ZipRejected(f"path traversal (zip-slip) in archive: {raw!r}")
    return str(p)


def _allowed(name: str) -> bool:
    base = os.path.basename(name).lower()
    ext = os.path.splitext(base)[1]
    return ext in ALLOWED_EXT or base in ALLOWED_NAMES


def safe_extract(zip_path: str, dest_dir: str, limits: ZipLimits | None = None) -> ZipReport:
    lim = limits or ZipLimits()
    archive_size = max(os.path.getsize(zip_path), 1)
    report = ZipReport()
    dest_root = os.path.realpath(dest_dir)
    os.makedirs(dest_root, exist_ok=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ZipRejected(f"not a valid zip: {e}")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(zf.infolist()) > lim.max_files:
            raise ZipRejected(f"too many entries ({len(zf.infolist())} > {lim.max_files})")
        declared = sum(i.file_size for i in infos)
        if declared > lim.max_total_bytes:
            raise ZipRejected(f"declared size {declared} exceeds {lim.max_total_bytes}")
        if declared / archive_size > lim.max_ratio:
            raise ZipRejected(f"compression ratio {declared / archive_size:.0f} exceeds {lim.max_ratio} (zip bomb)")

        total = 0
        for info in infos:
            name = _clean_name(info.filename)
            if info.flag_bits & 0x1:
                raise ZipRejected(f"encrypted entry: {name}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ZipRejected(f"symlink in archive: {name}")
            ext = os.path.splitext(name)[1].lower()
            if ext in EXECUTABLE_EXT:
                report.flagged_executables.append(name)
                continue
            if not _allowed(name):
                report.skipped.append(name)
                continue
            target = os.path.realpath(os.path.join(dest_root, name))
            if os.path.commonpath([dest_root, target]) != dest_root:
                raise ZipRejected(f"path escapes destination: {name}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            written = 0
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    while chunk := src.read(64 * 1024):
                        written += len(chunk)
                        total += len(chunk)
                        # enforced on real decompressed bytes, not on the (forgeable) header
                        if written > lim.max_member_bytes:
                            raise ZipRejected(f"member {name} exceeds {lim.max_member_bytes} bytes")
                        if total > lim.max_total_bytes or total / archive_size > lim.max_ratio:
                            raise ZipRejected("extracted size/ratio over limit (zip bomb)")
                        out.write(chunk)
            except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError) as e:
                raise ZipRejected(f"corrupt or unsupported entry {name}: {e}")
            report.extracted.append(name)
    return report
