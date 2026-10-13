"""Golden-set calibration: LLM score vs hand-assigned expected score, per criterion.

    python -m app.calibrate            # uses LLM_PROVIDER from the environment (default: mock)

The expected scores are hand-assigned by the POC author for 3 tiny projects. With the mock provider
this measures the pipeline plumbing and heuristics, NOT the quality of a real model.
"""
import json
from decimal import Decimal
from pathlib import Path

from app import db
from app.auth import seed_users
from app.config import get_settings
from app.models import create_schema
from app.rubrics import seed_rubric
from app.runner import evaluate_directory, latest_result

GOLDEN = Path(__file__).resolve().parents[1] / "golden"


def calibrate_rows() -> list[dict]:
    expected = json.loads((GOLDEN / "expected.json").read_text())
    rows = []
    for name in sorted(k for k in expected if not k.startswith("_")):
        job_id, status = evaluate_directory(GOLDEN / name, f"golden-{name}")
        res = latest_result(job_id)
        got = {c["key"]: (Decimal(c["score"]) if c["score"] is not None else None) for c in res.breakdown}
        rows.append({
            "project": name, "job_status": status, "review_reasons": res.review_reasons,
            "criteria": {k: {"expected": Decimal(str(v)), "got": got.get(k),
                             "delta": (got[k] - Decimal(str(v))) if got.get(k) is not None else None}
                         for k, v in expected[name].items() if k != "total"},
            "total": {"expected": Decimal(str(expected[name]["total"])), "got": Decimal(str(res.total_score)),
                      "delta": Decimal(str(res.total_score)) - Decimal(str(expected[name]["total"]))},
        })
    return rows


def render(rows: list[dict]) -> str:
    out = [f"provider={get_settings().llm_provider}", ""]
    out.append(f"{'project':<18}{'criterion':<15}{'expected':>9}{'got':>8}{'delta':>8}")
    out.append("-" * 58)
    for r in rows:
        for k, v in r["criteria"].items():
            d = "n/a" if v["delta"] is None else f"{v['delta']:+.1f}"
            g = "n/a" if v["got"] is None else f"{v['got']:.1f}"
            out.append(f"{r['project']:<18}{k:<15}{v['expected']:>9.1f}{g:>8}{d:>8}")
        t = r["total"]
        out.append(f"{r['project']:<18}{'TOTAL (0-100)':<15}{t['expected']:>9.1f}{t['got']:>8.1f}{t['delta']:>+8.1f}")
        out.append(f"{'':<18}-> job {r['job_status']}; review reasons: {r['review_reasons'] or 'none'}")
        out.append("")
    mae = sum(abs(r["total"]["delta"]) for r in rows) / len(rows)
    out.append(f"mean absolute total delta: {mae:.1f} points over {len(rows)} projects")
    return "\n".join(out)


def main() -> None:
    engine = db.get_engine()
    create_schema(engine)
    with db.session_scope() as s:
        seed_users(s)
        seed_rubric(s)
    print(render(calibrate_rows()))


if __name__ == "__main__":
    main()
