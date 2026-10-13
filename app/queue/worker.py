"""Postgres-backed job queue: claim / heartbeat / checkpoint / fail / reap.

State machine:  queued -> running -> (needs_review | completed | failed | dead)
                running -> queued   (retry after error or lease expiry)
                running -> dead     (attempts exhausted: acts as the DLQ)

Every write by a worker is conditional on (status='running' AND locked_by=<me>), so a worker whose
lease was reaped can never overwrite the state of the worker that took over (layer 3 idempotency).
"""
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.models import Job, Outbox

log = logging.getLogger("worker")


class LeaseLost(Exception):
    """This worker no longer owns the job (lease expired and was reaped / re-claimed)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def claim_job(worker: str, lease_seconds: int | None = None) -> Job | None:
    """SELECT ... FOR UPDATE SKIP LOCKED, then a conditional UPDATE guarded by status and version."""
    lease = lease_seconds if lease_seconds is not None else get_settings().job_lease_seconds
    with session_scope() as s:
        job = s.scalar(
            select(Job).where(Job.status == "queued").order_by(Job.created_at)
            .with_for_update(skip_locked=True).limit(1)
        )
        if job is None:
            return None
        now = _now()
        claimed = s.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "queued", Job.version == job.version)
            .values(
                status="running", locked_by=worker, attempts=Job.attempts + 1, version=Job.version + 1,
                lease_expires_at=now + timedelta(seconds=lease), heartbeat_at=now, updated_at=now,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        ).first()
        if claimed is None:
            return None
        s.execute(
            update(Outbox).where(Outbox.job_id == job.id, Outbox.dispatched_at.is_(None)).values(dispatched_at=now)
        )
        s.expire_all()
        return s.get(Job, job.id)


class VersionConflict(LeaseLost):
    """Optimistic-lock failure: the row changed since this writer read it (its `version` is stale)."""


def _owned_update(s: Session, job_id: str, worker: str, *, expected_version: int | None = None, **values) -> None:
    """Conditional write: only the lease owner, and (when given) only if `version` is still what the caller read."""
    now = _now()
    conds = [Job.id == job_id, Job.status == "running", Job.locked_by == worker]
    if expected_version is not None:
        conds.append(Job.version == expected_version)
    row = s.execute(
        update(Job).where(*conds)
        .values(version=Job.version + 1, updated_at=now, **values)
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).first()
    if row is None:
        if expected_version is not None:
            owner = s.execute(select(Job.status, Job.locked_by).where(Job.id == job_id)).first()
            if owner and owner.status == "running" and owner.locked_by == worker:
                raise VersionConflict(job_id)  # we still own it, but another writer got in first
        raise LeaseLost(job_id)


def heartbeat(job_id: str, worker: str, lease_seconds: int | None = None) -> None:
    lease = lease_seconds if lease_seconds is not None else get_settings().job_lease_seconds
    now = _now()
    with session_scope() as s:
        _owned_update(s, job_id, worker, heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease))


def save_checkpoint(job_id: str, worker: str, stage: str, output: dict, expected_version: int | None = None) -> None:
    """Read-modify-write of the checkpoints JSON, guarded by the optimistic `version` column so a stale
    writer cannot overwrite checkpoints written since it read the row (raises VersionConflict)."""
    now = _now()
    lease = get_settings().job_lease_seconds
    with session_scope() as s:
        job = s.get(Job, job_id)
        cps = {**(job.checkpoints or {}), stage: output}
        _owned_update(
            s, job_id, worker, expected_version=job.version if expected_version is None else expected_version,
            checkpoints=cps, stage=stage,
            heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease),
        )


def release_failed(job_id: str, worker: str, error: str) -> str:
    """Error in a stage: requeue for retry, or move to 'dead' when attempts are exhausted."""
    max_attempts = get_settings().job_max_attempts
    with session_scope() as s:
        job = s.get(Job, job_id)
        new_status = "dead" if job.attempts >= max_attempts else "queued"
        _owned_update(
            s, job_id, worker, status=new_status, last_error=error[:2000],
            locked_by=None, lease_expires_at=None,
        )
        return new_status


def reap_expired(now: datetime | None = None) -> list[tuple[str, str]]:
    """Requeue running jobs whose lease expired; dead-letter those out of attempts."""
    now = now or _now()
    max_attempts = get_settings().job_max_attempts
    out = []
    with session_scope() as s:
        jobs = s.scalars(
            select(Job).where(Job.status == "running", Job.lease_expires_at < now).with_for_update(skip_locked=True)
        ).all()
        for j in jobs:
            j.status = "dead" if j.attempts >= max_attempts else "queued"
            j.last_error = f"lease expired (worker {j.locked_by})"
            j.locked_by = None
            j.lease_expires_at = None
            j.version += 1
            out.append((j.id, j.status))
    return out


@dataclass
class StageContext:
    job_id: str
    submission_id: str
    rubric_id: str
    worker: str
    prior: dict = field(default_factory=dict)  # outputs of earlier stages

    def heartbeat(self) -> None:
        heartbeat(self.job_id, self.worker)


StageFn = Callable[[StageContext], dict]
Finalizer = Callable[[Session, Job, dict], str]  # returns final status: completed | needs_review | failed


def run_job(job: Job, worker: str, stages: list[tuple[str, StageFn]], finalize: Finalizer) -> str:
    """Run stages in order, skipping those already checkpointed (resume after crash)."""
    ctx = StageContext(job.id, job.submission_id, job.rubric_id, worker, dict(job.checkpoints or {}))
    try:
        for name, fn in stages:
            if name in ctx.prior:
                log.info("skip finished stage", extra={"job_id": job.id, "submission_id": job.submission_id, "stage": name})
                continue
            t0 = time.monotonic()
            out = fn(ctx)
            save_checkpoint(job.id, worker, name, out)
            log.info("stage done", extra={"job_id": job.id, "submission_id": job.submission_id, "stage": name,
                                          "duration_ms": int((time.monotonic() - t0) * 1000)})
            ctx.prior[name] = out
        with session_scope() as s:
            j = s.get(Job, job.id)
            final = finalize(s, j, ctx.prior)  # creates the result in the same transaction
            _owned_update(s, job.id, worker, status=final, locked_by=None, lease_expires_at=None)
            return final
    except LeaseLost:
        log.warning("lease lost", extra={"job_id": job.id, "submission_id": job.submission_id})
        return "lease_lost"
    except Exception as e:  # noqa: BLE001 - any stage error is a retryable job failure
        log.exception("stage failed", extra={"job_id": job.id, "submission_id": job.submission_id})
        try:
            return release_failed(job.id, worker, f"{type(e).__name__}: {e}")
        except LeaseLost:
            return "lease_lost"


def run_once(worker: str, stages: list[tuple[str, StageFn]], finalize: Finalizer) -> str | None:
    job = claim_job(worker)
    if job is None:
        return None
    return run_job(job, worker, stages, finalize)


def main() -> None:  # pragma: no cover - long-running loop
    from app.logging_setup import setup_logging
    from app.pipeline.stages import FINALIZE, STAGES  # imported lazily: pipeline needs the DB

    setup_logging()

    wid = worker_id()
    last_reap = 0.0
    while True:
        if time.time() - last_reap > 10:
            for jid, st in reap_expired():
                log.warning("reaped", extra={"job_id": jid, "status": st})
            last_reap = time.time()
        if run_once(wid, STAGES, FINALIZE) is None:
            time.sleep(1)


if __name__ == "__main__":
    main()
