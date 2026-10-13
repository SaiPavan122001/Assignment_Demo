"""Scoring is plain code on Decimal. The LLM only proposes per-criterion scores; it never aggregates."""
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


def aggregate(criteria: list[dict]) -> dict:
    """criteria items: {key, weight, max_score, score|None}. Returns total on a 0-100 scale.

    total = 100 * sum(weight_i * score_i / max_i). A criterion without a score contributes 0 and
    marks the total `provisional` (it will be routed to a human anyway).
    """
    total = Decimal(0)
    provisional = False
    for c in criteria:
        if c.get("score") is None:
            provisional = True
            continue
        total += Decimal(str(c["weight"])) * Decimal(str(c["score"])) / Decimal(str(c["max_score"]))
    total = (total * 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return {"total": str(total), "max": "100.00", "provisional": provisional}
