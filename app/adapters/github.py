"""Public GitHub repo via `git clone --depth 1`. Static reading only: nothing in the repo is executed.

Only https://github.com/<owner>/<repo> URLs are accepted (host allowlist + SSRF validation), so the
clone cannot be pointed at internal hosts. Hooks are not transferred by clone; we additionally
disable them and credential prompts.
"""
import os
import re
import shutil
import subprocess
import tempfile

from app.adapters.base import FAILED, OK, PARTIAL, AdapterResult, register
from app.adapters.common import read_tree
from app.security.ssrf import SSRFBlocked, validate_url

_URL_RX = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
CLONE_TIMEOUT = 60
MAX_REPO_BYTES = 100 * 1024 * 1024


def _clone(url: str, dest: str) -> None:
    """Separate function so tests can substitute a local repo for the network clone."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-c", "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
         "-c", "protocol.file.allow=never", "clone", "--depth", "1", "--single-branch", "--no-tags", url, dest],
        check=True, capture_output=True, timeout=CLONE_TIMEOUT, env=env,
    )


def _dir_size(path: str) -> int:
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


@register("github")
def fetch_github(artifact, storage) -> AdapterResult:
    url = (artifact.source_ref or "").strip()
    m = _URL_RX.match(url)
    if not m:
        return AdapterResult(FAILED, "only https://github.com/<owner>/<repo> URLs are accepted")
    try:
        validate_url(url)
    except SSRFBlocked as e:
        return AdapterResult(FAILED, f"blocked: {e}")
    if shutil.which("git") is None:
        return AdapterResult(FAILED, "git is not installed on the worker")
    tmp = tempfile.mkdtemp(prefix="gh-")
    try:
        try:
            _clone(url, os.path.join(tmp, "repo"))
        except subprocess.TimeoutExpired:
            return AdapterResult(FAILED, f"clone timed out after {CLONE_TIMEOUT}s")
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or b"").decode(errors="replace").strip().splitlines()
            return AdapterResult(FAILED, "clone failed (private or missing repo?): " + (msg[-1][:150] if msg else ""))
        repo = os.path.join(tmp, "repo")
        if _dir_size(repo) > MAX_REPO_BYTES:
            return AdapterResult(FAILED, "repository larger than size cap")
        files, notes = read_tree(repo)
        if not files:
            return AdapterResult(FAILED, "repository has no readable text files")
        meta = {"owner": m.group(1), "repo": m.group(2)}
        return AdapterResult(PARTIAL if notes else OK, "; ".join(notes[:3]), files, meta)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
