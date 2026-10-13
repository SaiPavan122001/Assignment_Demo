"""LLMGateway: the only way the pipeline talks to a model.

Retries with backoff on transient provider errors, per-evaluation token budget, response cache keyed
by sha256(system + prompt + rubric_version + model), and a row in `llm_calls` for every call.
Production counterpart: a managed model endpoint behind the same interface.
"""
import hashlib
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select

from app.config import get_settings
from app.db import session_scope
from app.llm.providers import LLMProvider, ProviderError, approx_tokens, get_provider
from app.models import LLMCache, LLMCall

log = logging.getLogger("llm")

# Illustrative reference price used ONLY to estimate cost (USD per 1M tokens). Not a vendor quote.
# Actual spend for mock / local ollama / gemini free tier is $0.
REFERENCE_PRICE_IN = Decimal("1.00")
REFERENCE_PRICE_OUT = Decimal("5.00")


class BudgetExceeded(Exception):
    pass


@dataclass
class GatewayResponse:
    text: str
    tokens_in: int
    tokens_out: int
    cache_hit: bool


def estimate_cost(tokens_in: int, tokens_out: int) -> Decimal:
    return (Decimal(tokens_in) * REFERENCE_PRICE_IN + Decimal(tokens_out) * REFERENCE_PRICE_OUT) / Decimal(1_000_000)


class LLMGateway:
    def __init__(self, provider: LLMProvider | None = None, *, max_retries: int = 3, backoff_s: float = 0.5,
                 token_budget: int | None = None):
        self.provider = provider or get_provider()
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.token_budget = token_budget if token_budget is not None else get_settings().llm_token_budget_per_eval

    @staticmethod
    def cache_key(system: str, prompt: str, rubric_version: str, model: str) -> str:
        return hashlib.sha256("\x1f".join([system, prompt, str(rubric_version), model]).encode()).hexdigest()

    def tokens_used(self, job_id: str | None) -> int:
        if not job_id:
            return 0
        with session_scope() as s:
            return int(s.scalar(select(func.coalesce(func.sum(LLMCall.tokens_in + LLMCall.tokens_out), 0))
                                .where(LLMCall.job_id == job_id)) or 0)

    def _log(self, job_id, purpose, key, tin, tout, latency_ms, cache_hit, ok):
        with session_scope() as s:
            s.add(LLMCall(
                job_id=job_id, purpose=purpose, provider=self.provider.name, model=self.provider.model,
                prompt_hash=key, tokens_in=tin, tokens_out=tout, latency_ms=latency_ms,
                cost_usd=Decimal(0) if cache_hit else estimate_cost(tin, tout), cache_hit=cache_hit, ok=ok,
            ))

    def complete(self, *, prompt: str, system: str = "", purpose: str, job_id: str | None = None,
                 rubric_version: str | int = "") -> GatewayResponse:
        key = self.cache_key(system, prompt, str(rubric_version), self.provider.model)
        cached: GatewayResponse | None = None
        with session_scope() as s:
            hit = s.get(LLMCache, key)
            if hit:
                cached = GatewayResponse(hit.response, hit.tokens_in, hit.tokens_out, True)
        if cached:
            self._log(job_id, purpose, key, 0, 0, 0, True, True)  # cache hits consume no budget
            return cached

        used = self.tokens_used(job_id)
        if used + approx_tokens(system + prompt) > self.token_budget:
            raise BudgetExceeded(f"token budget {self.token_budget} would be exceeded (used {used})")

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            t0 = time.monotonic()
            try:
                r = self.provider.complete(prompt, system)
            except ProviderError as e:
                last_err = e
                self._log(job_id, purpose, key, 0, 0, int((time.monotonic() - t0) * 1000), False, False)
                if not e.retryable:
                    break
                time.sleep(self.backoff_s * (2 ** attempt))
                continue
            latency = int((time.monotonic() - t0) * 1000)
            with session_scope() as s:
                if not s.get(LLMCache, key):
                    s.add(LLMCache(key=key, response=r.text, tokens_in=r.tokens_in, tokens_out=r.tokens_out))
            self._log(job_id, purpose, key, r.tokens_in, r.tokens_out, latency, False, True)
            return GatewayResponse(r.text, r.tokens_in, r.tokens_out, False)
        raise ProviderError(f"LLM call failed after {self.max_retries} attempts: {last_err}")
