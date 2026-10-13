"""Cost report: measured tokens/cost per evaluation + linear projection for 30 and 10,000 submissions/day.

    python -m app.cost

Costs are ESTIMATES at an illustrative reference price (see app.llm.gateway), not vendor quotes.
Real spend with the mock provider, a local Ollama model or Gemini's free tier is $0.
"""
from decimal import Decimal

from sqlalchemy import func, select

from app import db
from app.llm.gateway import REFERENCE_PRICE_IN, REFERENCE_PRICE_OUT, estimate_cost
from app.models import Job, LLMCall

SCALES = (30, 10_000)


def measure() -> list[dict]:
    """One row per evaluation that made LLM calls."""
    with db.session_scope() as s:
        rows = s.execute(
            select(
                LLMCall.job_id,
                func.count().label("calls"),
                func.count().filter(LLMCall.cache_hit).label("cache_hits"),
                func.sum(LLMCall.tokens_in).label("tin"),
                func.sum(LLMCall.tokens_out).label("tout"),
                func.sum(LLMCall.cost_usd).label("cost"),
            ).where(LLMCall.job_id.is_not(None)).group_by(LLMCall.job_id)
        ).all()
        status = dict(s.execute(select(Job.id, Job.status)).all())
    return [{"job_id": r.job_id, "status": status.get(r.job_id, "?"), "calls": r.calls, "cache_hits": r.cache_hits,
             "tokens_in": int(r.tin or 0), "tokens_out": int(r.tout or 0), "cost": Decimal(r.cost or 0)} for r in rows]


def projection(avg_in: Decimal, avg_out: Decimal, avg_calls: Decimal, scales=SCALES) -> list[dict]:
    out = []
    for n in scales:
        cost_day = estimate_cost(int(avg_in * n), int(avg_out * n))
        out.append({"submissions_per_day": n, "calls_per_day": int(avg_calls * n),
                    "tokens_per_day": int((avg_in + avg_out) * n), "cost_per_day": cost_day,
                    "cost_per_30_days": cost_day * 30,
                    "avg_calls_per_second": (avg_calls * n / Decimal(86400))})
    return out


def render(rows: list[dict]) -> str:
    if not rows:
        return "No LLM calls recorded yet. Run `python tasks.py demo` or `python tasks.py calibrate` first."
    out = [f"Reference price (assumption, not a quote): ${REFERENCE_PRICE_IN}/1M input tokens, "
           f"${REFERENCE_PRICE_OUT}/1M output tokens", ""]
    out.append(f"{'evaluation':<38}{'status':<14}{'calls':>6}{'cached':>7}{'tok in':>9}{'tok out':>9}{'est $':>10}")
    out.append("-" * 93)
    for r in rows:
        out.append(f"{r['job_id']:<38}{r['status']:<14}{r['calls']:>6}{r['cache_hits']:>7}"
                   f"{r['tokens_in']:>9}{r['tokens_out']:>9}{r['cost']:>10.5f}")
    n = Decimal(len(rows))
    avg_in = sum(Decimal(r["tokens_in"]) for r in rows) / n
    avg_out = sum(Decimal(r["tokens_out"]) for r in rows) / n
    avg_calls = sum(Decimal(r["calls"] - r["cache_hits"]) for r in rows) / n
    out += ["", f"Average per evaluation over {len(rows)}: {avg_in:.0f} tokens in, {avg_out:.0f} tokens out, "
                f"{avg_calls:.1f} uncached LLM calls", "", "Projection (linear, no retries, no cache hits):",
            f"{'submissions/day':>16}{'LLM calls/day':>15}{'tokens/day':>14}{'est $/day':>12}{'est $/30d':>12}{'calls/s avg':>13}"]
    for p in projection(avg_in, avg_out, avg_calls):
        out.append(f"{p['submissions_per_day']:>16,}{p['calls_per_day']:>15,}{p['tokens_per_day']:>14,}"
                   f"{p['cost_per_day']:>12.2f}{p['cost_per_30_days']:>12.2f}{p['avg_calls_per_second']:>13.2f}")
    out += ["", "Caveats: token counts here come from the mock provider's chars/4 estimate and its short outputs;",
            "a real model will differ. The projection is arithmetic, not a capacity test: this POC was NOT run",
            "at 10,000/day and a single Postgres-queue worker would not be sized for it."]
    return "\n".join(out)


def main() -> None:
    print(render(measure()))


if __name__ == "__main__":
    main()
