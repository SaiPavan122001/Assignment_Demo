from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select

from app.db import session_scope
from app.models import Job, Outbox, Submission
from app.queue.outbox import enqueue_evaluation
from tests.helpers import add_readme_artifact, make_submission


def test_create_submission_same_key_twice_gives_one_submission(api, engine):
    a = make_submission(api, key="same")
    b = make_submission(api, key="same")
    assert a["id"] == b["id"]
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Submission)) == 1


def test_different_key_creates_new_submission(api):
    assert make_submission(api, key="k1")["id"] != make_submission(api, key="k2")["id"]


def test_idempotency_key_is_scoped_per_user(api):
    a = make_submission(api, "student1@example.com", key="same")
    b = make_submission(api, "student2@example.com", key="same")
    assert a["id"] != b["id"]


def test_missing_idempotency_key_rejected(api):
    r = api.c.post("/submissions", json={"title": "x"}, headers=api.h("student1@example.com"))
    assert r.status_code == 422


def test_start_evaluation_five_times_creates_one_job(api, engine):
    sub = make_submission(api)
    add_readme_artifact(api, sub["id"])
    h = api.h("student1@example.com")
    responses = [api.c.post(f"/submissions/{sub['id']}/evaluations", headers=h) for _ in range(5)]
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert [r.json()["created"] for r in responses] == [True, False, False, False, False]
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.scalar(select(func.count()).select_from(Outbox)) == 1


def test_concurrent_start_creates_one_active_job(api, engine):
    sub = make_submission(api)
    add_readme_artifact(api, sub["id"])
    with session_scope() as s:
        rubric_id = s.scalar(select(Job.rubric_id)) or api.c.get("/submissions", headers=api.h("admin@example.com")) and None
    from app.models import Rubric
    with session_scope() as s:
        rubric_id = s.scalar(select(Rubric.id))
        user_id = s.scalar(select(Submission.owner_id))

    n = 8
    barrier = Barrier(n)

    def attempt(_):
        with session_scope() as s:
            barrier.wait()  # all threads race into enqueue at once
            job, created = enqueue_evaluation(s, sub["id"], rubric_id, user_id)
            return job.id, created

    with ThreadPoolExecutor(n) as ex:
        results = list(ex.map(attempt, range(n)))
    assert len({jid for jid, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1
        assert s.scalar(select(func.count()).select_from(Outbox)) == 1


def test_evaluation_without_artifacts_rejected(api):
    sub = make_submission(api)
    r = api.c.post(f"/submissions/{sub['id']}/evaluations", headers=api.h("student1@example.com"))
    assert r.status_code == 422
