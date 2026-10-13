import shutil
import tempfile

from app.adapters.base import FAILED, OK, PARTIAL, AdapterResult, register
from app.adapters.common import read_tree
from app.security.zipsafe import ZipRejected, safe_extract


@register("zip")
def fetch_zip(artifact, storage) -> AdapterResult:
    if not artifact.storage_key:
        return AdapterResult(FAILED, "no uploaded file")
    tmp = tempfile.mkdtemp(prefix="zip-")  # extraction happens in a throwaway dir, removed on any outcome
    try:
        try:
            report = safe_extract(storage.local_path(artifact.storage_key), tmp)
        except ZipRejected as e:
            return AdapterResult(FAILED, f"zip rejected: {e}")
        files, notes = read_tree(tmp)
        if report.flagged_executables:
            notes.append(f"{len(report.flagged_executables)} executable file(s) skipped")
        if report.skipped:
            notes.append(f"{len(report.skipped)} file(s) outside allowlist skipped")
        if not files:
            return AdapterResult(FAILED, "archive contains no readable text files")
        status = PARTIAL if any("cap" in n or "executable" in n or "truncated" in n for n in notes) else OK
        return AdapterResult(status, "; ".join(notes[:4]), files,
                             {"skipped": report.skipped[:50], "executables": report.flagged_executables[:50]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
