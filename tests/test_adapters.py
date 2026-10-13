import http.server
import io
import subprocess
import threading
import zipfile

import docx
import httpx
import pytest

from app.adapters import base, google_drive, github, video
from app.models import Artifact
from app.storage.local import LocalStorage


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(str(tmp_path / "store"))


def art(type_, **kw):
    return Artifact(type=type_, meta=kw.pop("meta", {}), **kw)


def minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return bytes(out)


# 1. github (clone replaced by a local repo; the URL/validation/tree-reading path is real)
def test_github_adapter_reads_cloned_repo(tmp_path, storage, monkeypatch):
    src = tmp_path / "src"
    (src / "app").mkdir(parents=True)
    (src / "app" / "main.py").write_text("print('hello')\n")
    (src / "README.md").write_text("# Demo\n")
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "node_modules" / "x" / "i.js").write_text("ignored")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"], check=True)

    def fake_clone(url, dest):
        subprocess.run(["git", "clone", "-q", "--depth", "1", src.as_uri(), dest], check=True, capture_output=True)

    monkeypatch.setattr(github, "_clone", fake_clone)
    monkeypatch.setattr(github, "validate_url", lambda u: u)  # no DNS in tests
    r = base.fetch(art("github", source_ref="https://github.com/octo/demo"), storage)
    assert r.status == "ok", r.reason
    assert set(r.files) == {"app/main.py", "README.md"}  # node_modules ignored, .git ignored


def test_github_adapter_rejects_non_github_and_ssrf(storage):
    for ref in ("https://evil.example/a/b", "http://github.com/a/b", "https://127.0.0.1/a/b", "file:///etc"):
        r = base.fetch(art("github", source_ref=ref), storage)
        assert r.status == "failed", ref


# 2. zip
def test_zip_adapter_ok_and_rejects_slip(tmp_path, storage):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("proj/app.py", "x = 1")
        z.writestr("proj/README.md", "# p")
    key = storage.put(buf.getvalue(), ".zip")
    r = base.fetch(art("zip", storage_key=key, source_ref="p.zip"), storage)
    assert r.status == "ok" and set(r.files) == {"proj/app.py", "proj/README.md"}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../evil.py", "boom")
    r = base.fetch(art("zip", storage_key=storage.put(buf.getvalue(), ".zip")), storage)
    assert r.status == "failed" and "zip rejected" in r.reason


# 3. pdf
def test_pdf_adapter_extracts_text(storage):
    key = storage.put(minimal_pdf("Project report hello"), ".pdf")
    r = base.fetch(art("pdf", storage_key=key, source_ref="report.pdf"), storage)
    assert r.status == "ok", r.reason
    assert "Project report hello" in r.files["report.pdf.txt"]


def test_pdf_garbage_is_failed_not_exception(storage):
    r = base.fetch(art("pdf", storage_key=storage.put(b"not a pdf", ".pdf")), storage)
    assert r.status == "failed"


# 4. documentation
def test_documentation_md_and_docx(tmp_path, storage):
    r = base.fetch(art("documentation", storage_key=storage.put(b"# Hi\nbody", ".md"), source_ref="README.md"), storage)
    assert r.status == "ok" and "body" in r.files["README.md"]
    d = docx.Document()
    d.add_paragraph("Design document paragraph")
    p = tmp_path / "d.docx"
    d.save(p)
    r = base.fetch(art("documentation", storage_key=storage.put(p.read_bytes(), ".docx"), source_ref="d.docx"), storage)
    assert r.status == "ok" and "Design document paragraph" in r.files["d.docx"]


# 5. live_url (real local HTTP server; guard's test-only escape hatch enabled just for this test)
@pytest.fixture
def local_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/missing":
                self.send_response(404); self.end_headers(); return
            body = b"<html><head><title>My App</title><style>.x{}</style></head><body><h1>Welcome</h1><script>var secret=1</script><p>Todo list demo</p></body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_live_url_blocked_by_default(local_server, storage):
    r = base.fetch(art("live_url", source_ref=local_server + "/"), storage)
    assert r.status == "failed" and "blocked" in r.reason


def test_live_url_extracts_visible_text(local_server, storage, monkeypatch):
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE", "true")
    r = base.fetch(art("live_url", source_ref=local_server + "/"), storage)
    assert r.status == "ok", r.reason
    text = r.files["live_url.txt"]
    assert r.meta["status_code"] == 200 and r.meta["title"] == "My App"
    assert "Welcome" in text and "Todo list demo" in text and "secret" not in text
    r404 = base.fetch(art("live_url", source_ref=local_server + "/missing"), storage)
    assert r404.status == "failed" and "404" in r404.reason


# 6. google_drive
def test_drive_fixture_public_and_private(storage, monkeypatch):
    monkeypatch.setattr(google_drive, "get_settings", lambda: type("S", (), {"google_api_key": ""})())
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    pub = base.fetch(art("google_drive", source_ref="https://drive.google.com/drive/folders/public-demo-folder"), storage)
    assert pub.status == "fixture" and "design.md" in pub.files
    priv = base.fetch(art("google_drive", source_ref="private-demo-folder"), storage)
    assert priv.status == "skipped" and priv.reason == "auth required" and not priv.files


def test_drive_real_path_with_mocked_api(storage, monkeypatch):
    """Exercises request/parse logic against canned Drive API responses. NOT a test against real Google."""
    def handler(req: httpx.Request):
        p = req.url.path
        if p.endswith("/files/FOLDER"):
            return httpx.Response(200, json={"id": "FOLDER", "name": "f", "mimeType": google_drive.GOOGLE_FOLDER})
        if p.endswith("/files/PRIV"):
            return httpx.Response(404, json={"error": {"code": 404}})
        if p.endswith("/files"):
            return httpx.Response(200, json={"files": [
                {"id": "D1", "name": "Design", "mimeType": google_drive.GOOGLE_DOC},
                {"id": "I1", "name": "pic.png", "mimeType": "image/png"},
            ]})
        if p.endswith("/files/D1/export"):
            return httpx.Response(200, text="Exported design text")
        return httpx.Response(500)

    real = google_drive.safe_get
    monkeypatch.setattr(google_drive, "safe_get", lambda url, **kw: real(
        url, resolver=lambda h: ["142.250.1.1"], transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(google_drive, "get_settings", lambda: type("S", (), {"google_api_key": "K"})())
    r = base.fetch(art("google_drive", source_ref="https://drive.google.com/drive/folders/FOLDER"), storage)
    assert r.status == "partial" and r.files == {"Design": "Exported design text"}
    r = base.fetch(art("google_drive", source_ref="https://drive.google.com/file/d/PRIV/view"), storage)
    assert r.status == "skipped" and r.reason == "auth required"


# 7. video
def test_video_with_srt_transcript_ok(storage):
    srt = "1\n00:00:01,000 --> 00:00:04,000\nHello this is my demo\n\n2\n00:00:05,000 --> 00:00:09,000\nIt shows the dashboard\n"
    a = art("video", storage_key=storage.put(b"\x00fakevideo", ".mp4"), source_ref="demo.mp4",
            meta={"transcript_key": storage.put(srt.encode(), ".srt")})
    r = base.fetch(a, storage)
    assert r.status == "ok" and "Hello this is my demo" in r.files["video_transcript.txt"]
    assert r.meta["duration_s"] == 9.0  # derived from last cue when ffprobe is absent


def test_video_without_transcript_is_partial(storage, monkeypatch):
    monkeypatch.delenv("WHISPER_ENABLED", raising=False)
    r = base.fetch(art("video", storage_key=storage.put(b"\x00v", ".mp4"), source_ref="d.mp4"), storage)
    assert r.status == "partial" and "no transcript" in r.reason and not r.files


def test_video_over_20_minutes_skipped(storage, monkeypatch):
    monkeypatch.setattr(video, "_probe_duration", lambda p: 21 * 60.0)
    r = base.fetch(art("video", storage_key=storage.put(b"\x00v", ".mp4"), source_ref="long.mp4"), storage)
    assert r.status == "skipped" and "20 min" in r.reason


def test_video_over_20_minutes_detected_from_transcript_when_no_ffprobe(storage, monkeypatch):
    monkeypatch.setattr(video, "_probe_duration", lambda p: None)
    srt = "1\n00:25:00,000 --> 00:25:03,000\nlate cue\n"
    a = art("video", storage_key=storage.put(b"\x00v", ".mp4"), meta={"transcript_key": storage.put(srt.encode(), ".srt")})
    assert base.fetch(a, storage).status == "skipped"


def test_unknown_adapter_type_is_failed_not_exception(storage):
    assert base.fetch(art("hologram"), storage).status == "failed"
