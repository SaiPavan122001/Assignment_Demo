"""In-process helper used by calibrate / cost / demo: submit a project directory and run the worker."""
import io
import zipfile
from pathlib import Path

from sqlalchemy import select

from app.db import session_scope
from app.models import Artifact, Job, Result, Submission, User
from app.pipeline.stages import FINALIZE, STAGES
from app.queue.outbox import enqueue_evaluation
from app.queue.worker import run_once
from app.rubrics import get_latest_published
from app.storage.local import LocalStorage


def zip_directory(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                z.writestr(f.relative_to(path).as_posix(), f.read_bytes())
    return buf.getvalue()


def evaluate_directory(path: Path, title: str, owner_email: str = "student1@example.com") -> tuple[str, str]:
    """Returns (evaluation_id, final job status)."""
    key = LocalStorage().put(zip_directory(path), ".zip")
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == owner_email))
        sub = Submission(owner_id=owner.id, title=title)
        s.add(sub)
        s.flush()
        s.add(Artifact(submission_id=sub.id, type="zip", source_ref=f"{title}.zip", storage_key=key, meta={}))
        rubric = get_latest_published(s)
        job, _ = enqueue_evaluation(s, sub.id, rubric.id, owner.id)
        job_id = job.id
    while True:
        out = run_once("inproc", STAGES, FINALIZE)
        with session_scope() as s:
            status = s.get(Job, job_id).status
        if status not in ("queued", "running") or out is None:
            return job_id, status


def latest_result(job_id: str) -> Result:
    with session_scope() as s:
        return s.scalar(select(Result).where(Result.job_id == job_id).order_by(Result.version.desc()).limit(1))
