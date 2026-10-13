import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.llm.gateway import BudgetExceeded, LLMGateway
from app.llm.providers import LLMProvider, MockProvider, ProviderError, ProviderResponse
from app.models import LLMCall


class Flaky(LLMProvider):
    name, model = "flaky", "flaky-1"

    def __init__(self, fail_times, retryable=True):
        self.fail_times, self.calls, self.retryable = fail_times, 0, retryable

    def complete(self, prompt, system=""):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError("503", retryable=self.retryable)
        return ProviderResponse('{"ok": true}', 100, 20)


def calls():
    with session_scope() as s:
        return s.scalars(select(LLMCall).order_by(LLMCall.id)).all()


def test_call_is_logged_with_tokens_latency_cost(engine):
    gw = LLMGateway(Flaky(0), backoff_s=0)
    r = gw.complete(prompt="hello", purpose="t", job_id="j1", rubric_version=1)
    assert r.text == '{"ok": true}' and not r.cache_hit
    [c] = calls()
    assert (c.tokens_in, c.tokens_out, c.cache_hit, c.ok, c.provider) == (100, 20, False, True, "flaky")
    assert c.cost_usd == Decimal("0.000200")  # 100*1/1e6 + 20*5/1e6


def test_cache_hit_costs_nothing_and_skips_provider(engine):
    p = Flaky(0)
    gw = LLMGateway(p, backoff_s=0)
    gw.complete(prompt="same", purpose="t", job_id="j1", rubric_version=1)
    r = gw.complete(prompt="same", purpose="t", job_id="j1", rubric_version=1)
    assert r.cache_hit and p.calls == 1
    assert [c.cache_hit for c in calls()] == [False, True]
    assert calls()[1].cost_usd == 0


def test_cache_key_includes_rubric_version(engine):
    p = Flaky(0)
    gw = LLMGateway(p, backoff_s=0)
    gw.complete(prompt="same", purpose="t", job_id="j", rubric_version=1)
    gw.complete(prompt="same", purpose="t", job_id="j", rubric_version=2)
    assert p.calls == 2


def test_retries_transient_errors_then_succeeds(engine):
    p = Flaky(2)
    r = LLMGateway(p, backoff_s=0).complete(prompt="x", purpose="t", job_id="j")
    assert p.calls == 3 and r.text
    assert [c.ok for c in calls()] == [False, False, True]


def test_gives_up_after_max_retries(engine):
    p = Flaky(99)
    with pytest.raises(ProviderError, match="after 3 attempts"):
        LLMGateway(p, backoff_s=0).complete(prompt="x", purpose="t", job_id="j")
    assert p.calls == 3


def test_non_retryable_error_not_retried(engine):
    p = Flaky(99, retryable=False)
    with pytest.raises(ProviderError):
        LLMGateway(p, backoff_s=0).complete(prompt="x", purpose="t", job_id="j")
    assert p.calls == 1


def test_per_evaluation_token_budget_enforced(engine):
    gw = LLMGateway(Flaky(0), backoff_s=0, token_budget=150)
    gw.complete(prompt="a", purpose="t", job_id="j1")  # uses 120 tokens
    with pytest.raises(BudgetExceeded):
        gw.complete(prompt="b" * 200, purpose="t", job_id="j1")
    gw.complete(prompt="c", purpose="t", job_id="j2")  # budget is per evaluation


def test_mock_provider_is_deterministic_and_offline():
    prompt = "TASK: summarize_file\nPATH: a.py\n<<<STUDENT_DATA source=a.py\n# Title line\nx=1\nSTUDENT_DATA>>>\n"
    a, b = MockProvider().complete(prompt), MockProvider().complete(prompt)
    assert a == b and json.loads(a.text)["summary"] == "Title line"
