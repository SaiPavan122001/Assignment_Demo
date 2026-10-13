"""Results, evaluator review queue, approve / override / reject (all audited) and publish."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.submissions import load_submission
from app.auth import STAFF, current_user, require_roles
from app.db import get_session
from app.models import AuditLog, Job, Result, User
from app.pipeline.aggregate import aggregate

router = APIRouter(tags=["review"])
staff_only = require_roles(*STAFF)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=2000)


class OverrideIn(BaseModel):
    scores: dict[str, str]  # criterion key -> new score (as string to stay exact)
    note: str = Field(min_length=3, max_length=2000)


def _public_breakdown(breakdown: list[dict]) -> list[dict]:
    return [{k: c.get(k) for k in ("key", "name", "score", "max_score", "weight", "justification", "evidence")}
            for c in breakdown]


def _result_out(r: Result, *, staff: bool) -> dict:
    out = {
        "id": r.id, "evaluation_id": r.job_id, "version": r.version, "status": r.status,
        "total_score": str(r.total_score) if r.total_score is not None else None,
        "max_score": str(r.max_score) if r.max_score is not None else None,
        "published_at": r.published_at,
    }
    if staff:
        out.update(breakdown=r.breakdown, warnings=r.warnings, review_reasons=r.review_reasons,
                   created_by=r.created_by, note=r.note, created_at=r.created_at)
    else:
        out.update(breakdown=_public_breakdown(r.breakdown), warnings=r.warnings)
    return out


def _audit(s: Session, actor: User, action: str, job_id: str, **detail) -> None:
    s.add(AuditLog(actor_id=actor.id, action=action, target_type="job", target_id=job_id, detail=detail))


def _locked_job(s: Session, evaluation_id: str) -> Job:
    job = s.scalar(select(Job).where(Job.id == evaluation_id).with_for_update())
    if job is None:
        raise HTTPException(404, "evaluation not found")
    return job


def _latest(s: Session, job_id: str) -> Result | None:
    return s.scalar(select(Result).where(Result.job_id == job_id).order_by(Result.version.desc()).limit(1))


def _bump(job: Job, status: str, error: str | None = None) -> None:
    job.status = status
    job.version += 1
    if error is not None:
        job.last_error = error


@router.get("/evaluations/{evaluation_id}/result")
def get_result(evaluation_id: str, user: User = Depends(current_user), s: Session = Depends(get_session)):
    job = s.get(Job, evaluation_id)
    if job is None:
        raise HTTPException(404, "evaluation not found")
    load_submission(s, job.submission_id, user)  # ownership: students only reach their own
    if user.role in STAFF:
        versions = s.scalars(select(Result).where(Result.job_id == job.id).order_by(Result.version)).all()
        if not versions:
            raise HTTPException(404, "no result yet")
        return {"latest": _result_out(versions[-1], staff=True),
                "versions": [_result_out(v, staff=True) for v in versions]}
    pub = s.scalar(select(Result).where(Result.job_id == job.id, Result.status == "published"))
    if pub is None:
        raise HTTPException(404, "result not published")  # same answer whether it exists or not
    return _result_out(pub, staff=False)


@router.get("/review/queue")
def review_queue(user: User = Depends(staff_only), s: Session = Depends(get_session)):
    jobs = s.scalars(select(Job).where(Job.status == "needs_review").order_by(Job.created_at)).all()
    out = []
    for j in jobs:
        r = _latest(s, j.id)
        out.append({"evaluation_id": j.id, "submission_id": j.submission_id, "created_at": j.created_at,
                    "total_score": str(r.total_score) if r else None, "review_reasons": r.review_reasons if r else []})
    return out


@router.post("/evaluations/{evaluation_id}/approve")
def approve(evaluation_id: str, body: NoteIn, user: User = Depends(staff_only), s: Session = Depends(get_session)):
    job = _locked_job(s, evaluation_id)
    r = _latest(s, job.id)
    if r is None or r.status != "draft":
        raise HTTPException(409, "latest result is not awaiting review")
    r.status, r.note = "approved", body.note
    _bump(job, "completed")
    _audit(s, user, "approve", job.id, version=r.version, note=body.note)
    return _result_out(r, staff=True)


@router.post("/evaluations/{evaluation_id}/override", status_code=201)
def override(evaluation_id: str, body: OverrideIn, user: User = Depends(staff_only), s: Session = Depends(get_session)):
    """Creates a NEW result version; earlier versions are kept (superseded) for the audit trail."""
    job = _locked_job(s, evaluation_id)
    cur = _latest(s, job.id)
    if cur is None or cur.status in ("rejected",):
        raise HTTPException(409, "no reviewable result")
    breakdown = [dict(c) for c in cur.breakdown]
    by_key = {c["key"]: c for c in breakdown}
    for key, raw in body.scores.items():
        if key not in by_key:
            raise HTTPException(422, f"unknown criterion {key!r}")
        try:
            val = Decimal(raw)
        except InvalidOperation:
            raise HTTPException(422, f"score for {key!r} is not a number")
        if val < 0 or val > Decimal(str(by_key[key]["max_score"])):
            raise HTTPException(422, f"score for {key!r} must be within 0..{by_key[key]['max_score']}")
        c = by_key[key]
        c.update(previous_score=c.get("score"), score=str(val), overridden_by=user.id, needs_review=False)
    agg = aggregate(breakdown)
    new = Result(job_id=job.id, version=cur.version + 1, status="approved", total_score=agg["total"],
                 max_score=agg["max"], breakdown=breakdown, warnings=cur.warnings, review_reasons=cur.review_reasons,
                 created_by=user.id, note=body.note)
    if cur.status != "published":
        cur.status = "superseded"  # a published version stays published until the new one is published
    s.add(new)
    _bump(job, "completed")
    _audit(s, user, "override", job.id, from_version=cur.version, to_version=new.version,
           scores=body.scores, note=body.note)
    s.flush()
    return _result_out(new, staff=True)


@router.post("/evaluations/{evaluation_id}/reject")
def reject(evaluation_id: str, body: NoteIn, user: User = Depends(staff_only), s: Session = Depends(get_session)):
    job = _locked_job(s, evaluation_id)
    r = _latest(s, job.id)
    if r is None or r.status not in ("draft", "approved"):
        raise HTTPException(409, "latest result cannot be rejected")
    r.status, r.note = "rejected", body.note
    _bump(job, "failed", f"rejected by evaluator: {body.note}"[:2000])
    _audit(s, user, "reject", job.id, version=r.version, note=body.note)
    return _result_out(r, staff=True)


@router.post("/evaluations/{evaluation_id}/publish")
def publish(evaluation_id: str, user: User = Depends(staff_only), s: Session = Depends(get_session)):
    """Exactly one published result per evaluation (uq_one_published_result backs the checks here)."""
    job = _locked_job(s, evaluation_id)
    r = _latest(s, job.id)
    if r is None:
        raise HTTPException(404, "no result")
    if r.status == "published":
        raise HTTPException(409, "already published")
    if r.status != "approved":
        raise HTTPException(409, f"result is '{r.status}'; only an approved result can be published")
    try:
        with s.begin_nested():
            s.execute(update(Result).where(Result.job_id == job.id, Result.status == "published")
                      .values(status="superseded"))
            r.status, r.published_at = "published", datetime.now(timezone.utc)
            s.flush()
    except IntegrityError:
        raise HTTPException(409, "already published")
    _audit(s, user, "publish", job.id, version=r.version)
    return _result_out(r, staff=True)
