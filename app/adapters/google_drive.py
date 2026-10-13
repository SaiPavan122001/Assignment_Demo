"""Google Drive: real for PUBLIC files/folders when GOOGLE_API_KEY is set; otherwise local fixtures.

- key set: Drive API v3 (files.list / files.get / export / alt=media) via the SSRF-guarded fetcher.
  403/404 means the item is not public -> status 'skipped', reason 'auth required' (no OAuth is attempted).
- no key: fixtures/drive/<id>/ is read and the result is marked status 'fixture'.
"""
import json
import os
import re
from pathlib import Path

from app.adapters.base import FAILED, FIXTURE, OK, PARTIAL, SKIPPED, AdapterResult, register
from app.adapters.common import MAX_FILE_BYTES, read_tree
from app.config import get_settings
from app.security.ssrf import SSRFBlocked, safe_get

API = "https://www.googleapis.com/drive/v3"
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "drive"
MAX_DRIVE_FILES = 30
TEXT_MIME_PREFIXES = ("text/",)
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_FOLDER = "application/vnd.google-apps.folder"

_ID_RX = [re.compile(r"/folders/([A-Za-z0-9_-]+)"), re.compile(r"/d/([A-Za-z0-9_-]+)"), re.compile(r"[?&]id=([A-Za-z0-9_-]+)")]


def parse_drive_id(ref: str) -> str | None:
    ref = ref.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", ref):
        return ref
    for rx in _ID_RX:
        m = rx.search(ref)
        if m:
            return m.group(1)
    return None


def _fixture(drive_id: str) -> AdapterResult:
    d = FIXTURE_ROOT / drive_id
    manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())
    entry = manifest.get(drive_id)
    if entry is None or not d.is_dir():
        return AdapterResult(SKIPPED, "no GOOGLE_API_KEY configured and no fixture for this id")
    if not entry.get("public", False):
        return AdapterResult(SKIPPED, "auth required")
    files, _ = read_tree(str(d))
    return AdapterResult(FIXTURE, "GOOGLE_API_KEY not set: served from local fixture, not Google", files,
                         {"drive_id": drive_id})


def _get_json(path: str, params: str, key: str) -> tuple[int, dict]:
    res = safe_get(f"{API}/{path}?{params}&key={key}", max_bytes=2 * 1024 * 1024)
    try:
        return res.status_code, json.loads(res.body or b"{}")
    except ValueError:
        return res.status_code, {}


def _real(drive_id: str, key: str) -> AdapterResult:
    status, meta = _get_json(f"files/{drive_id}", "fields=id,name,mimeType", key)
    if status in (401, 403, 404):
        return AdapterResult(SKIPPED, "auth required")
    if status != 200:
        return AdapterResult(FAILED, f"Drive API HTTP {status}")
    items = [meta]
    if meta.get("mimeType") == GOOGLE_FOLDER:
        q = f"q='{drive_id}'+in+parents+and+trashed=false&fields=files(id,name,mimeType,size)&pageSize={MAX_DRIVE_FILES}"
        status, listing = _get_json("files", q, key)
        if status != 200:
            return AdapterResult(SKIPPED if status in (401, 403, 404) else FAILED,
                                 "auth required" if status in (401, 403, 404) else f"Drive API HTTP {status}")
        items = listing.get("files", [])
    files: dict[str, str] = {}
    skipped = 0
    for it in items[:MAX_DRIVE_FILES]:
        mime, fid, name = it.get("mimeType", ""), it["id"], it.get("name", it["id"])
        if mime == GOOGLE_DOC:
            url = f"{API}/files/{fid}/export?mimeType=text/plain&key={key}"
        elif mime.startswith(TEXT_MIME_PREFIXES) or name.lower().endswith((".md", ".py", ".js", ".json")):
            url = f"{API}/files/{fid}?alt=media&key={key}"
        else:
            skipped += 1
            continue
        res = safe_get(url, max_bytes=MAX_FILE_BYTES)
        if res.status_code == 200:
            files[name] = res.body.decode("utf-8", errors="replace")
        else:
            skipped += 1
    if not files:
        return AdapterResult(PARTIAL if items else FAILED, "no readable text/Docs files in Drive item", {},
                             {"drive_id": drive_id})
    return AdapterResult(PARTIAL if skipped else OK, f"{skipped} non-text file(s) skipped" if skipped else "",
                         files, {"drive_id": drive_id})


@register("google_drive")
def fetch_drive(artifact, storage) -> AdapterResult:
    drive_id = parse_drive_id(artifact.source_ref or "")
    if not drive_id:
        return AdapterResult(FAILED, "could not parse a Drive file/folder id")
    key = get_settings().google_api_key or os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        return _fixture(drive_id)
    try:
        return _real(drive_id, key)
    except SSRFBlocked as e:
        return AdapterResult(FAILED, f"blocked: {e}")
