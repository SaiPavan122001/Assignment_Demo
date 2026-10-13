"""Human-review routing. Pure function: easy to test, easy to audit."""
import hashlib
from decimal import Decimal

LOW_CONFIDENCE = 0.6
BOUNDARIES = [Decimal(x) for x in (40, 60, 75, 90)]  # grade cut-offs on the 0-100 scale
BOUNDARY_MARGIN = Decimal("2.0")
AUDIT_PERCENT = 10  # deterministic sample by job id: reproducible and unbiasable by the student


def audit_sampled(job_id: str, percent: int | None = None) -> bool:
    percent = AUDIT_PERCENT if percent is None else percent  # module value read at call time
    return int(hashlib.sha256(job_id.encode()).hexdigest(), 16) % 100 < percent


def route(*, job_id: str, total: str, provisional: bool, criteria: list[dict], artifacts: list[dict],
          injection_flags: dict, usable_content: bool, audit_percent: int | None = None) -> list[str]:
    reasons: list[str] = []
    if not usable_content:
        reasons.append("no_usable_content")
    for c in criteria:
        if c.get("needs_review"):
            reasons.append(f"criterion_needs_review:{c['key']}({c.get('review_reason', '')})")
        elif c.get("confidence") is not None and c["confidence"] < LOW_CONFIDENCE:
            reasons.append(f"low_confidence:{c['key']}={c['confidence']:.2f}")
        if c.get("rejected_evidence"):
            reasons.append(f"evidence_rejected:{c['key']}")
    for a in artifacts:
        if a["status"] in ("failed", "skipped"):
            reasons.append(f"artifact_{a['status']}:{a['type']}({a.get('reason', '')})")
    if injection_flags:
        reasons.append("injection_flag:" + ",".join(sorted(injection_flags)))
    t = Decimal(total)
    if not provisional and any(abs(t - b) <= BOUNDARY_MARGIN for b in BOUNDARIES):
        reasons.append(f"near_boundary:{t}")
    if audit_sampled(job_id, audit_percent):
        reasons.append("random_audit_sample")
    return reasons
