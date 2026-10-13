import io
import json
import zipfile
from pathlib import Path

from app.db import session_scope
from app.llm.gateway import LLMGateway
from app.models import Job, Result
from app.pipeline import stages
from app.queue import worker as w
from sqlalchemy import select

GOLDEN = Path(__file__).resolve().parents[1] / "golden"


def zip_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                z.writestr(f.relative_to(path).as_posix(), f.read_bytes())
    return buf.getvalue()


def zip_files(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def submit(api, artifacts: list[tuple[str, str, bytes]], email="student1@example.com", key="k"):
    """artifacts: [(type, filename, data)]. Returns (submission_id, evaluation_id)."""
    r = api.c.post("/submissions", json={"title": "t"}, headers=api.h(email, **{"Idempotency-Key": key}))
    sub = r.json()["id"]
    for typ, name, data in artifacts:
        r = api.c.post(f"/submissions/{sub}/artifacts", data={"type": typ},
                       files={"file": (name, data)}, headers=api.h(email))
        assert r.status_code == 201, r.text
    r = api.c.post(f"/submissions/{sub}/evaluations", headers=api.h(email))
    assert r.status_code == 202, r.text
    return sub, r.json()["id"]


def run_worker(provider=None) -> str:
    if provider is not None:
        stages.GATEWAY_FACTORY = lambda: LLMGateway(provider, backoff_s=0)
    try:
        return w.run_once("test-worker", stages.STAGES, stages.FINALIZE)
    finally:
        stages.GATEWAY_FACTORY = LLMGateway


def get_result(job_id, version=1) -> Result:
    with session_scope() as s:
        return s.scalar(select(Result).where(Result.job_id == job_id, Result.version == version))


def get_job(job_id) -> Job:
    with session_scope() as s:
        return s.get(Job, job_id)
