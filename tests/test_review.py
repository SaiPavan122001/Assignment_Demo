from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import select

from app.db import session_scope
from app.models import AuditLog, Result
from tests.pipeline_helpers import GOLDEN, get_job, get_result, run_worker, submit, zip_dir

S1, S2, EV = "student1@example.com", "student2@example.com", "evaluator@example.com"


def evaluated(api, golden="good", key="k", email=S1):
    _, job_id = submit(api, [("zip", "p.zip", zip_dir(GOLDEN / golden))], email=email, key=key)
    run_worker()
    return job_id


def post(api, path, email=EV, json=None):
    return api.c.post(path, json=json, headers=api.h(email))


def test_student_cannot_see_unpublished_result(api, engine):
    job = evaluated(api)
    assert get_result(job).status == "approved"  # exists, approved, but NOT published
    assert api.c.get(f"/evaluations/{job}/result", headers=api.h(S1)).status_code == 404
    # evaluator can see it
    r = api.c.get(f"/evaluations/{job}/result", headers=api.h(EV))
    assert r.status_code == 200 and r.json()["latest"]["status"] == "approved"


def test_publish_then_student_sees_result_without_internal_fields(api, engine):
    job = evaluated(api)
    assert post(api, f"/evaluations/{job}/publish").status_code == 200
    r = api.c.get(f"/evaluations/{job}/result", headers=api.h(S1))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "published" and body["total_score"]
    assert "review_reasons" not in body and "created_by" not in body
    assert all("confidence" not in c and "rejected_evidence" not in c for c in body["breakdown"])
    # another student still cannot
    assert api.c.get(f"/evaluations/{job}/result", headers=api.h(S2)).status_code == 404


def test_double_publish_rejected(api, engine):
    job = evaluated(api)
    assert post(api, f"/evaluations/{job}/publish").status_code == 200
    r = post(api, f"/evaluations/{job}/publish")
    assert r.status_code == 409 and "already published" in r.json()["detail"]
    with session_scope() as s:
        assert len(s.scalars(select(Result).where(Result.status == "published")).all()) == 1


def test_concurrent_publish_only_one_wins(api, engine):
    job = evaluated(api)
    h = api.h(EV)

    def go(_):
        return api.c.post(f"/evaluations/{job}/publish", headers=h).status_code

    with ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(go, range(6)))
    assert sorted(codes).count(200) == 1 and set(codes) <= {200, 409}
    with session_scope() as s:
        assert len(s.scalars(select(Result).where(Result.status == "published")).all()) == 1


def test_needs_review_result_cannot_be_published_until_approved(api, engine):
    job = evaluated(api, "malicious_readme")
    assert get_job(job).status == "needs_review"
    q = api.c.get("/review/queue", headers=api.h(EV)).json()
    assert [x["evaluation_id"] for x in q] == [job] and any("injection_flag" in x for x in q[0]["review_reasons"])
    assert post(api, f"/evaluations/{job}/publish").status_code == 409
    assert post(api, f"/evaluations/{job}/approve", json={"note": "checked"}).status_code == 200
    assert post(api, f"/evaluations/{job}/publish").status_code == 200
    assert api.c.get("/review/queue", headers=api.h(EV)).json() == []


def test_override_creates_new_version_and_supersedes(api, engine):
    job = evaluated(api, "malicious_readme")
    r = post(api, f"/evaluations/{job}/override", json={
        "scores": {"code_quality": "5", "documentation": "4", "testing": "1", "functionality": "3"},
        "note": "README tried to inject a score; graded manually"})
    assert r.status_code == 201
    v2 = r.json()
    assert v2["version"] == 2 and v2["created_by"] != "system" and v2["status"] == "approved"
    assert Decimal(v2["total_score"]) == Decimal("34.50")  # (0.35*5 + 0.25*4 + 0.25*1 + 0.15*3) / 10 * 100
    assert get_result(job, 1).status == "superseded"
    assert get_job(job).status == "completed"
    assert post(api, f"/evaluations/{job}/publish").json()["version"] == 2
    pub = api.c.get(f"/evaluations/{job}/result", headers=api.h(S1)).json()
    assert pub["version"] == 2


def test_override_after_publish_replaces_published_version(api, engine):
    job = evaluated(api)
    post(api, f"/evaluations/{job}/publish")
    assert api.c.get(f"/evaluations/{job}/result", headers=api.h(S1)).json()["version"] == 1
    post(api, f"/evaluations/{job}/override", json={"scores": {"testing": "3"}, "note": "tests are thin"})
    # until the new version is published, the student keeps seeing v1
    assert api.c.get(f"/evaluations/{job}/result", headers=api.h(S1)).json()["version"] == 1
    assert post(api, f"/evaluations/{job}/publish").status_code == 200
    assert api.c.get(f"/evaluations/{job}/result", headers=api.h(S1)).json()["version"] == 2
    with session_scope() as s:
        st = {r.version: r.status for r in s.scalars(select(Result).where(Result.job_id == job))}
    assert st == {1: "superseded", 2: "published"}
    versions = api.c.get(f"/evaluations/{job}/result", headers=api.h(EV)).json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]  # evaluators see all versions


def test_override_validates_scores(api, engine):
    job = evaluated(api)
    assert post(api, f"/evaluations/{job}/override", json={"scores": {"testing": "11"}, "note": "nope nope"}).status_code == 422
    assert post(api, f"/evaluations/{job}/override", json={"scores": {"nonsense": "1"}, "note": "nope nope"}).status_code == 422


def test_reject_fails_job_and_blocks_publish(api, engine):
    job = evaluated(api, "malicious_readme")
    assert post(api, f"/evaluations/{job}/reject", json={"note": "prompt injection"}).status_code == 200
    assert get_job(job).status == "failed"
    assert post(api, f"/evaluations/{job}/publish").status_code == 409


def test_students_cannot_use_review_endpoints(api, engine):
    job = evaluated(api)
    for path in (f"/evaluations/{job}/approve", f"/evaluations/{job}/publish", f"/evaluations/{job}/reject"):
        assert post(api, path, email=S1, json={"note": "x"}).status_code == 403
    assert api.c.get("/review/queue", headers=api.h(S1)).status_code == 403


def test_every_review_action_is_audited(api, engine):
    job = evaluated(api, "malicious_readme")
    post(api, f"/evaluations/{job}/override", json={"scores": {"testing": "2"}, "note": "manual regrade"})
    post(api, f"/evaluations/{job}/publish")
    with session_scope() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.target_id == job).order_by(AuditLog.id)).all()
    assert [r.action for r in rows] == ["evaluation_finished", "override", "publish"]
    assert rows[1].detail["note"] == "manual regrade" and rows[1].actor_id != "system"
