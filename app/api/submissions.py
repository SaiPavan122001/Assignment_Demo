from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth import STAFF, current_user
from app.db import get_session
from app.models import Artifact, IdempotencyKey, Job, Rubric, Submission, User
from app.queue.outbox import enqueue_evaluation
from app.rubrics import get_latest_published
from app.storage.local import get_storage

router = APIRouter(tags=["submissions"])

ARTIFACT_TYPES = {"github", "zip", "pdf", "documentation", "live_url", "google_drive", "video"}
URL_ONLY = {"github", "live_url"}
FILE_ONLY = {"zip", "pdf", "documentation"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class SubmissionIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class EvaluationIn(BaseModel):
    rubric_id: str | None = None


def load_submission(s: Session, submission_id: str, user: User) -> Submission:
    """Ownership check. Students get 404 (not 403) for other people's submissions."""
    sub = s.get(Submission, submission_id)
    if sub is None or (user.role not in STAFF and sub.owner_id != user.id):
        raise HTTPException(404, "submission not found")
    return sub


def _sub_out(sub: Submission) -> dict:
    return {"id": sub.id, "title": sub.title, "owner_id": sub.owner_id, "created_at": sub.created_at}


@router.post("/submissions", status_code=201)
def create_submission(
    body: SubmissionIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
    user: User = Depends(current_user),
    s: Session = Depends(get_session),
):
    """Layer 1 idempotency: the key row is inserted first with ON CONFLICT DO NOTHING."""
    import uuid
    new_id = str(uuid.uuid4())
    res = s.execute(
        pg_insert(IdempotencyKey)
        .values(user_id=user.id, scope="create_submission", key=idempotency_key, resource_id=new_id)
        .on_conflict_do_nothing()
        .returning(IdempotencyKey.key)  # rowcount is unreliable here; RETURNING is empty on conflict
    )
    if res.first() is None:
        row = s.get(IdempotencyKey, (user.id, "create_submission", idempotency_key))
        return _sub_out(s.get(Submission, row.resource_id))
    sub = Submission(id=new_id, owner_id=user.id, title=body.title)
    s.add(sub)
    s.flush()
    return _sub_out(sub)


@router.get("/submissions")
def list_submissions(user: User = Depends(current_user), s: Session = Depends(get_session)):
    q = select(Submission).order_by(Submission.created_at.desc())
    if user.role not in STAFF:
        q = q.where(Submission.owner_id == user.id)
    return [_sub_out(x) for x in s.scalars(q)]


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: str, user: User = Depends(current_user), s: Session = Depends(get_session)):
    sub = load_submission(s, submission_id, user)
    arts = s.scalars(select(Artifact).where(Artifact.submission_id == sub.id)).all()
    return {
        **_sub_out(sub),
        "artifacts": [{"id": a.id, "type": a.type, "source_ref": a.source_ref} for a in arts],
    }


@router.post("/submissions/{submission_id}/artifacts", status_code=201)
async def add_artifact(
    submission_id: str,
    type: str = Form(...),
    url: str | None = Form(None),
    file: UploadFile | None = File(None),
    transcript: UploadFile | None = File(None),  # optional, video only
    user: User = Depends(current_user),
    s: Session = Depends(get_session),
):
    sub = load_submission(s, submission_id, user)
    if type not in ARTIFACT_TYPES:
        raise HTTPException(422, f"type must be one of {sorted(ARTIFACT_TYPES)}")
    if type in URL_ONLY and not url:
        raise HTTPException(422, f"{type} requires url")
    if type in FILE_ONLY and file is None:
        raise HTTPException(422, f"{type} requires a file upload")
    if type in {"google_drive", "video"} and not (url or file):
        raise HTTPException(422, f"{type} requires url or file")

    storage = get_storage()
    art = Artifact(submission_id=sub.id, type=type, source_ref=url, meta={})
    if file is not None:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        suffix = "." + file.filename.rsplit(".", 1)[-1] if file.filename and "." in file.filename else ""
        art.storage_key = storage.put(data, suffix)
        art.source_ref = url or file.filename
    if transcript is not None and type == "video":
        tdata = await transcript.read(MAX_UPLOAD_BYTES + 1)
        tsuffix = "." + transcript.filename.rsplit(".", 1)[-1] if transcript.filename and "." in transcript.filename else ".txt"
        art.meta = {"transcript_key": storage.put(tdata, tsuffix)}
    s.add(art)
    s.flush()
    return {"id": art.id, "type": art.type}


@router.post("/submissions/{submission_id}/evaluations", status_code=202)
def start_evaluation(
    submission_id: str,
    body: EvaluationIn | None = None,
    user: User = Depends(current_user),
    s: Session = Depends(get_session),
):
    """Layer 2 idempotency (uq_active_job): N calls -> one active job."""
    sub = load_submission(s, submission_id, user)
    if not s.scalar(select(Artifact.id).where(Artifact.submission_id == sub.id).limit(1)):
        raise HTTPException(422, "submission has no artifacts")
    rubric_id = body.rubric_id if body and body.rubric_id else None
    if rubric_id:
        rubric = s.get(Rubric, rubric_id)
        if rubric is None or rubric.status != "published":
            raise HTTPException(422, "rubric must exist and be published")
    else:
        rubric = get_latest_published(s)
        if rubric is None:
            raise HTTPException(409, "no published rubric")
    job, created = enqueue_evaluation(s, sub.id, rubric.id, user.id)
    return {"id": job.id, "status": job.status, "created": created, "rubric_id": rubric.id}


@router.get("/evaluations/{evaluation_id}")
def evaluation_status(evaluation_id: str, user: User = Depends(current_user), s: Session = Depends(get_session)):
    job = s.get(Job, evaluation_id)
    if job is None:
        raise HTTPException(404, "evaluation not found")
    load_submission(s, job.submission_id, user)  # ownership
    return {
        "id": job.id, "submission_id": job.submission_id, "status": job.status, "stage": job.stage,
        "attempts": job.attempts, "version": job.version, "last_error": job.last_error if user.role in STAFF else None,
    }
