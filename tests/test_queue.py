from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.config import get_settings
from app.db import session_scope
from app.models import Artifact, Job, Outbox
from app.queue import worker as w
from app.queue.outbox import enqueue_evaluation
from tests.helpers import add_readme_artifact, make_submission


class SimulatedCrash(BaseException):
    """BaseException so the runner's `except Exception` does not catch it: models a killed process."""


@pytest.fixture
def job_id(api, seeded):
    sub = make_submission(api)
    add_readme_artifact(api, sub["id"])
    r = api.c.post(f"/submissions/{sub['id']}/evaluations", headers=api.h("student1@example.com"))
    return r.json()["id"]


def finalize_ok(s, job, prior):
    return "completed"


def make_stages(calls, crash_in=None, fail_in=None):
    def stage(name):
        def fn(ctx):
            calls.append(name)
            if crash_in == name:
                raise SimulatedCrash()
            if fail_in == name:
                raise RuntimeError("boom")
            return {"stage": name}
        return fn
    return [(n, stage(n)) for n in ("extract", "facts", "summary")]


def expire_lease(job_id):
    with session_scope() as s:
        s.execute(
            text("update jobs set lease_expires_at = :t where id = :i"),
            {"t": datetime.now(timezone.utc) - timedelta(seconds=5), "i": job_id},
        )


def test_happy_path_completes_and_marks_outbox_dispatched(job_id):
    calls = []
    assert w.run_once("w1", make_stages(calls), finalize_ok) == "completed"
    assert calls == ["extract", "facts", "summary"]
    with session_scope() as s:
        j = s.get(Job, job_id)
        assert j.status == "completed" and j.stage == "summary" and j.attempts == 1
        assert j.version > 1
        assert s.scalar(select(Outbox.dispatched_at)) is not None


def test_crash_mid_stage_reaper_requeues_and_resumes_without_redoing_finished_stages(job_id):
    calls = []
    with pytest.raises(SimulatedCrash):
        w.run_once("w1", make_stages(calls, crash_in="facts"), finalize_ok)
    assert calls == ["extract", "facts"]
    with session_scope() as s:
        assert s.get(Job, job_id).status == "running"  # nobody cleaned up: the "process" died

    assert w.reap_expired() == []  # lease still valid -> nothing reaped
    expire_lease(job_id)
    assert w.reap_expired() == [(job_id, "queued")]

    calls2 = []
    assert w.run_once("w2", make_stages(calls2), finalize_ok) == "completed"
    assert calls2 == ["facts", "summary"]  # 'extract' was checkpointed and NOT redone
    with session_scope() as s:
        assert s.get(Job, job_id).attempts == 2


def test_job_becomes_dead_after_n_failures(job_id):
    n = get_settings().job_max_attempts
    results = []
    for _ in range(n):
        results.append(w.run_once("w1", make_stages([], fail_in="facts"), finalize_ok))
    assert results == ["queued"] * (n - 1) + ["dead"]
    with session_scope() as s:
        j = s.get(Job, job_id)
        assert j.status == "dead" and j.attempts == n and "boom" in j.last_error
    assert w.claim_job("w1") is None  # dead jobs are not claimable


def test_dead_letter_via_reaper_when_attempts_exhausted(job_id):
    n = get_settings().job_max_attempts
    for i in range(n):
        with pytest.raises(SimulatedCrash):
            w.run_once("w1", make_stages([], crash_in="extract"), finalize_ok)
        expire_lease(job_id)
        reaped = w.reap_expired()
    assert reaped == [(job_id, "dead")]


def test_stale_worker_cannot_overwrite_after_lease_lost(job_id):
    job = w.claim_job("w1")
    expire_lease(job_id)
    w.reap_expired()
    w.claim_job("w2")  # another worker took over
    with pytest.raises(w.LeaseLost):
        w.save_checkpoint(job.id, "w1", "extract", {"stale": True})
    with pytest.raises(w.LeaseLost):
        w.heartbeat(job.id, "w1")
    with session_scope() as s:
        assert s.get(Job, job_id).locked_by == "w2"
        assert "extract" not in (s.get(Job, job_id).checkpoints or {})


def test_optimistic_version_conflict_rejects_the_stale_writer(job_id):
    """Two writers with the same identity read the row at version v. The first write wins and bumps the
    version; the second, still holding v, is rejected and writes nothing."""
    job = w.claim_job("w1")
    v = job.version
    w.save_checkpoint(job.id, "w1", "extract", {"from": "writer A"}, expected_version=v)
    with session_scope() as s:
        assert s.get(Job, job_id).version == v + 1
    with pytest.raises(w.VersionConflict):
        w.save_checkpoint(job.id, "w1", "facts", {"from": "writer B (stale)"}, expected_version=v)
    with session_scope() as s:
        row = s.get(Job, job_id)
        assert row.version == v + 1 and set(row.checkpoints) == {"extract"}
    # re-reading the current version lets the second writer proceed
    w.save_checkpoint(job.id, "w1", "facts", {"from": "writer B"}, expected_version=v + 1)
    with session_scope() as s:
        assert set(s.get(Job, job_id).checkpoints) == {"extract", "facts"}


def test_version_conflict_is_a_lease_lost_subclass_so_the_runner_stops(job_id):
    assert issubclass(w.VersionConflict, w.LeaseLost)


def test_two_workers_cannot_claim_same_job(job_id):
    assert w.claim_job("w1") is not None
    assert w.claim_job("w2") is None
