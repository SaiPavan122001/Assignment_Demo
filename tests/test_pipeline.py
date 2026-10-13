import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.llm.providers import LLMProvider, MockProvider, ProviderResponse
from app.models import LLMCall
from app.pipeline.aggregate import aggregate
from app.pipeline.evidence import validate_evidence
from tests.pipeline_helpers import GOLDEN, get_job, get_result, run_worker, submit, zip_dir, zip_files


# ---------- pure units
def test_decimal_aggregation_is_exact():
    crit = [{"weight": "0.10", "max_score": "10", "score": "3"},
            {"weight": "0.20", "max_score": "10", "score": "3"},
            {"weight": "0.70", "max_score": "10", "score": "3"}]
    assert aggregate(crit) == {"total": "30.00", "max": "100.00", "provisional": False}
    # a float implementation gives 0.1*7.3+0.2*6.1+0.7*9.9 = 8.879999...; Decimal is exact
    crit = [{"weight": "0.10", "max_score": "10", "score": "7.3"},
            {"weight": "0.20", "max_score": "10", "score": "6.1"},
            {"weight": "0.70", "max_score": "10", "score": "9.9"}]
    assert aggregate(crit)["total"] == "88.80"


def test_aggregation_marks_missing_scores_provisional():
    r = aggregate([{"weight": "0.5", "max_score": "10", "score": "10"}, {"weight": "0.5", "max_score": "10", "score": None}])
    assert r == {"total": "50.00", "max": "100.00", "provisional": True}


def test_evidence_guard_units():
    files = {"a.py": "def hello():\n    return 1\n"}
    ok, bad = validate_evidence(
        [{"file": "a.py", "quote": "def hello():"}, {"file": "a.py", "quote": "def goodbye():"},
         {"file": "nope.py", "quote": "def hello():"}], files)
    assert [e["quote"] for e in ok] == ["def hello():"]
    assert len(bad) == 2


# ---------- end-to-end through API + worker + pipeline (mock provider)
def test_good_project_end_to_end(api, engine):
    _, job_id = submit(api, [("zip", "good.zip", zip_dir(GOLDEN / "good"))])
    assert run_worker() == "completed"
    j, r = get_job(job_id), get_result(job_id)
    assert j.status == "completed" and j.stage == "route"
    assert r.status == "approved" and r.review_reasons == [] and r.version == 1
    assert [c["key"] for c in r.breakdown] == ["code_quality", "documentation", "testing", "functionality"]
    assert Decimal(str(r.total_score)) > 60
    for c in r.breakdown:  # every accepted quote is literally in the project
        assert not c["rejected_evidence"]
    with session_scope() as s:
        purposes = [x.purpose for x in s.scalars(select(LLMCall).where(LLMCall.job_id == job_id))]
    assert sum(p.startswith("criterion:") for p in purposes) == 4  # exactly one call per criterion
    assert any(p.startswith("summary:") for p in purposes)


def test_malicious_readme_is_routed_to_review(api, engine):
    _, job_id = submit(api, [("zip", "m.zip", zip_dir(GOLDEN / "malicious_readme"))])
    assert run_worker() == "needs_review"
    r = get_result(job_id)
    assert any(x.startswith("injection_flag:") for x in r.review_reasons)
    assert r.status == "draft"  # never auto-approved
    # the gullible mock DID hand out full marks - detection, not the model, is what protects the score
    assert Decimal(str(r.total_score)) == Decimal("100.00")


def test_failed_artifact_is_a_warning_not_a_failure(api, engine):
    evil = zip_files({"../../evil.py": "boom"})
    readme = b"# Notes\nA short design note describing the system and how to run it."
    _, job_id = submit(api, [("zip", "evil.zip", evil), ("documentation", "NOTES.md", readme)])
    final = run_worker()
    assert final == "needs_review"  # routed to a human, not failed
    assert get_job(job_id).status == "needs_review"
    r = get_result(job_id)
    assert any("zip artifact failed" in x for x in r.warnings)
    assert any(x.startswith("artifact_failed:zip") for x in r.review_reasons)
    by_key = {c["key"]: c for c in r.breakdown}
    # criteria related to the failed code artifact had their confidence lowered; documentation did not
    assert by_key["code_quality"]["warnings"] and by_key["code_quality"]["confidence"] < 0.6
    assert not by_key["documentation"]["warnings"]


def test_all_artifacts_failed_goes_to_review_without_llm_calls(api, engine):
    _, job_id = submit(api, [("zip", "evil.zip", zip_files({"/abs.txt": "x"}))])
    assert run_worker() == "needs_review"
    r = get_result(job_id)
    assert "no_usable_content" in r.review_reasons
    with session_scope() as s:
        assert s.scalars(select(LLMCall).where(LLMCall.job_id == job_id)).all() == []


# ---------- model misbehaviour
class ScriptedProvider(LLMProvider):
    """Wraps the mock; a hook may rewrite criterion responses."""
    name, model = "scripted", "scripted-1"

    def __init__(self, criterion_hook):
        self.hook, self.mock, self.criterion_calls = criterion_hook, MockProvider(), 0

    def complete(self, prompt, system=""):
        r = self.mock.complete(prompt, system)
        if "TASK: criterion" in prompt:
            self.criterion_calls += 1
            text = self.hook(self.criterion_calls, prompt, r.text)
            return ProviderResponse(text, r.tokens_in, r.tokens_out)
        return r


def test_hallucinated_evidence_is_rejected_and_routed(api, engine):
    def hook(n, prompt, text):
        d = json.loads(text)
        d["evidence"] = [{"file": "app/main.py", "quote": "def totally_made_up_function():"}]
        return json.dumps(d)

    _, job_id = submit(api, [("zip", "good.zip", zip_dir(GOLDEN / "good"))])
    assert run_worker(ScriptedProvider(hook)) == "needs_review"
    r = get_result(job_id)
    assert all(c["rejected_evidence"] and not c["evidence"] for c in r.breakdown)
    assert all(c["confidence"] <= 0.3 for c in r.breakdown)
    assert any(x.startswith("evidence_rejected:") for x in r.review_reasons)


def test_invalid_json_retries_once_with_error_then_succeeds(api, engine):
    seen = []

    def hook(n, prompt, text):
        seen.append(prompt)
        return "this is not json" if n == 1 else text

    _, job_id = submit(api, [("zip", "good.zip", zip_dir(GOLDEN / "good"))])
    p = ScriptedProvider(hook)
    assert run_worker(p) == "completed"
    assert p.criterion_calls == 5  # 4 criteria + exactly ONE retry
    assert "PREVIOUS_RESPONSE_INVALID" in seen[1] and "not valid JSON" in seen[1]
    assert all(not c["needs_review"] for c in get_result(job_id).breakdown)


def test_persistently_invalid_output_marks_criterion_needs_review(api, engine):
    def hook(n, prompt, text):
        if "CRITERION: testing" in prompt:
            return json.dumps({"score": 99, "justification": "x", "confidence": 0.9})  # exceeds max_score
        return text

    _, job_id = submit(api, [("zip", "good.zip", zip_dir(GOLDEN / "good"))])
    p = ScriptedProvider(hook)
    assert run_worker(p) == "needs_review"
    by_key = {c["key"]: c for c in get_result(job_id).breakdown}
    assert by_key["testing"]["needs_review"] and by_key["testing"]["score"] is None
    assert by_key["testing"]["review_reason"].startswith("invalid_output")
    assert p.criterion_calls == 5  # tried twice, no third attempt
    assert get_result(job_id).total_score is not None  # provisional total still computed


def test_large_project_is_chunked_never_one_call(api, engine):
    files = {f"src/mod{i}.py": ("def f():\n    return %d\n" % i) * 700 for i in range(40)}  # ~15 KB each, 600 KB total
    files["README.md"] = "# Big\n## Usage\nrun it\n"
    _, job_id = submit(api, [("zip", "big.zip", zip_files(files))])
    assert run_worker() in ("completed", "needs_review")
    with session_scope() as s:
        calls = s.scalars(select(LLMCall).where(LLMCall.job_id == job_id)).all()
    assert max(c.tokens_in for c in calls) < 12_000  # ~48 KB of text at most per call, vs 600 KB project
    summary_calls = [c for c in calls if c.purpose.startswith("summary:")]
    assert len({c.purpose.split("#")[0] for c in summary_calls}) <= 12  # at most 12 files summarised
