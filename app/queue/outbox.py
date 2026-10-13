"""Transactional enqueue: job row + outbox row in ONE transaction, deduped by uq_active_job."""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, Outbox

ACTIVE = ("queued", "running")


def enqueue_evaluation(s: Session, submission_id: str, rubric_id: str, requested_by: str) -> tuple[Job, bool]:
    """Returns (job, created). Calling this N times yields exactly one active job."""
    for _ in range(3):
        existing = s.scalar(
            select(Job).where(Job.submission_id == submission_id, Job.rubric_id == rubric_id, Job.status.in_(ACTIVE))
        )
        if existing:
            return existing, False
        job = Job(submission_id=submission_id, rubric_id=rubric_id, requested_by=requested_by)
        try:
            with s.begin_nested():  # savepoint: a unique violation must not poison the outer transaction
                s.add(job)
                s.flush()
                s.add(Outbox(job_id=job.id, payload={"submission_id": submission_id, "rubric_id": rubric_id}))
                s.flush()
            return job, True
        except IntegrityError:
            continue  # lost the race against uq_active_job; loop will find the winner
    raise RuntimeError("could not enqueue evaluation")
