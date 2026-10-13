import json
import logging

from app.logging_setup import JsonFormatter
from app.queue import worker as w
from tests.pipeline_helpers import GOLDEN, run_worker, submit, zip_dir


def test_json_formatter_includes_job_and_submission_ids():
    rec = logging.LogRecord("worker", logging.INFO, "", 0, "stage done", (), None)
    rec.job_id, rec.submission_id = "J1", "S1"
    out = json.loads(JsonFormatter().format(rec))
    assert out["job_id"] == "J1" and out["submission_id"] == "S1" and out["msg"] == "stage done"


def test_worker_emits_structured_stage_logs(api, engine, caplog):
    caplog.set_level(logging.INFO, logger="worker")
    _, job_id = submit(api, [("zip", "g.zip", zip_dir(GOLDEN / "good"))])
    run_worker()
    done = [r for r in caplog.records if r.getMessage() == "stage done"]
    assert [r.stage for r in done] == ["extract", "facts", "summary", "criteria", "aggregate", "route"]
    assert all(r.job_id == job_id and r.submission_id for r in done)


def test_metrics_endpoint(api, engine):
    _, job_id = submit(api, [("zip", "g.zip", zip_dir(GOLDEN / "good"))])
    m = api.c.get("/metrics", headers=api.h("evaluator@example.com")).json()
    assert m["queue_depth"] == 1 and m["running"] == 0 and m["oldest_queued_age_seconds"] >= 0
    run_worker()
    m = api.c.get("/metrics", headers=api.h("evaluator@example.com")).json()
    assert m["queue_depth"] == 0 and m["jobs_by_status"] == {"completed": 1}
    assert m["llm"]["calls"] > 0 and m["llm"]["tokens_in"] > 0 and float(m["llm"]["estimated_cost_usd"]) > 0


def test_metrics_requires_staff(api):
    assert api.c.get("/metrics", headers=api.h("student1@example.com")).status_code == 403
    assert api.c.get("/metrics").status_code == 401
