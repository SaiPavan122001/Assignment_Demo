from app.pipeline import routing
from app.pipeline.routing import audit_sampled, route

OK_CRIT = [{"key": "a", "confidence": 0.9, "needs_review": False, "rejected_evidence": []}]


def r(total="70.00", **kw):
    base = dict(job_id="j", total=total, provisional=False, criteria=OK_CRIT,
                artifacts=[{"type": "zip", "status": "ok"}], injection_flags={}, usable_content=True)
    return route(**{**base, **kw})


def test_clean_result_is_not_routed():
    assert r() == []


def test_low_confidence_routes():
    crit = [{"key": "a", "confidence": 0.59, "needs_review": False, "rejected_evidence": []}]
    assert r(criteria=crit) == ["low_confidence:a=0.59"]


def test_failed_or_skipped_artifact_routes_but_partial_does_not():
    assert any(x.startswith("artifact_failed") for x in r(artifacts=[{"type": "pdf", "status": "failed", "reason": "x"}]))
    assert any(x.startswith("artifact_skipped") for x in r(artifacts=[{"type": "video", "status": "skipped"}]))
    assert r(artifacts=[{"type": "video", "status": "partial"}]) == []


def test_injection_flag_routes():
    assert r(injection_flags={"README.md": ["give_full_marks"]}) == ["injection_flag:README.md"]


def test_score_near_boundary_routes():
    assert r(total="59.00") == ["near_boundary:59.00"]   # within 2.0 of the 60 cut-off
    assert r(total="62.00") == ["near_boundary:62.00"]
    assert r(total="62.01") == []
    assert r(total="59.00", provisional=True) == []      # provisional totals are routed for other reasons


def test_random_audit_sample_is_deterministic_per_job(monkeypatch):
    assert audit_sampled("job-1", 100) is True and audit_sampled("job-1", 0) is False
    assert audit_sampled("job-1", 50) == audit_sampled("job-1", 50)  # same job, same answer
    monkeypatch.setattr(routing, "AUDIT_PERCENT", 100)
    assert r() == ["random_audit_sample"]
    hits = sum(audit_sampled(f"job-{i}", 10) for i in range(2000))
    assert 120 < hits < 280  # ~10% of 2000
