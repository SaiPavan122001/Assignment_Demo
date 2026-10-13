"""Pipeline stages: extract -> facts -> summary -> criteria -> aggregate -> route.

Each stage returns a JSON-able dict that the worker checkpoints, so a crashed worker resumes at the
last finished stage. Extracted text lives on disk (production: object store), not in the job row.
"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters import base as adapters
from app.db import session_scope
from app.llm.gateway import LLMGateway
from app.models import Artifact, AuditLog, Job, Result, Rubric
from app.pipeline.aggregate import aggregate
from app.pipeline.evaluate import evaluate_criterion
from app.pipeline.facts import compute_facts, scan_injection
from app.pipeline.routing import route
from app.pipeline.summarize import rank_files, summarize_files
from app.queue.worker import StageContext
from app.storage.local import LocalStorage

GATEWAY_FACTORY = LLMGateway  # tests / calibrate can replace this to inject a provider
CODE_TYPES = ("github", "zip")


def _workdir(job_id: str) -> Path:
    return LocalStorage().root / "extracted" / job_id


def load_files(job_id: str, manifest: dict[str, str]) -> dict[str, str]:
    wd = _workdir(job_id)
    return {virtual: (wd / disk).read_text("utf-8") for virtual, disk in manifest.items()}


def _rubric(rubric_id: str) -> Rubric:
    with session_scope() as s:
        return s.get(Rubric, rubric_id)


# ------------------------------------------------------------------ stages
def stage_extract(ctx: StageContext) -> dict:
    storage = LocalStorage()
    with session_scope() as s:
        arts = s.scalars(select(Artifact).where(Artifact.submission_id == ctx.submission_id)
                         .order_by(Artifact.created_at)).all()
    wd = _workdir(ctx.job_id)
    wd.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    summary = []
    first_code_taken = False
    for a in arts:
        ctx.heartbeat()
        res = adapters.fetch(a, storage)
        # the first code artifact keeps plain paths; everything else is namespaced by type
        prefix = ""
        if a.type in CODE_TYPES and not first_code_taken:
            first_code_taken = True
        else:
            prefix = f"{a.type}/"
        for path, text in res.files.items():
            virtual = prefix + path
            disk = f"{len(manifest)}.txt"
            (wd / disk).write_text(text, encoding="utf-8")
            manifest[virtual] = disk
        summary.append({"id": a.id, "type": a.type, "status": res.status, "reason": res.reason,
                        "n_files": len(res.files)})
    return {"artifacts": summary, "manifest": manifest}


def stage_facts(ctx: StageContext) -> dict:
    ex = ctx.prior["extract"]
    files = load_files(ctx.job_id, ex["manifest"])
    return {"facts": compute_facts(files), "injection_flags": scan_injection(files)}


def stage_summary(ctx: StageContext) -> dict:
    ex = ctx.prior["extract"]
    files = load_files(ctx.job_id, ex["manifest"])
    rubric = _rubric(ctx.rubric_id)
    summaries, selected, warnings = summarize_files(files, GATEWAY_FACTORY(), ctx.job_id, rubric.version)
    return {"summaries": summaries, "selected": selected, "warnings": warnings}


def stage_criteria(ctx: StageContext) -> dict:
    ex, facts = ctx.prior["extract"], ctx.prior["facts"]
    files = load_files(ctx.job_id, ex["manifest"])
    rubric = _rubric(ctx.rubric_id)
    gw = GATEWAY_FACTORY()
    ranked = rank_files(files)
    out = []
    for c in rubric.criteria:
        ctx.heartbeat()
        out.append(evaluate_criterion(c, rubric.version, files, ranked, facts["facts"],
                                      ctx.prior["summary"]["summaries"], ex["artifacts"], gw, ctx.job_id))
    return {"criteria": out}


def stage_aggregate(ctx: StageContext) -> dict:
    return aggregate(ctx.prior["criteria"]["criteria"])


def stage_route(ctx: StageContext) -> dict:
    ex = ctx.prior["extract"]
    agg = ctx.prior["aggregate"]
    reasons = route(
        job_id=ctx.job_id, total=agg["total"], provisional=agg["provisional"],
        criteria=ctx.prior["criteria"]["criteria"], artifacts=ex["artifacts"],
        injection_flags=ctx.prior["facts"]["injection_flags"], usable_content=bool(ex["manifest"]),
    )
    return {"review_reasons": reasons}


STAGES = [
    ("extract", stage_extract), ("facts", stage_facts), ("summary", stage_summary),
    ("criteria", stage_criteria), ("aggregate", stage_aggregate), ("route", stage_route),
]


def FINALIZE(s: Session, job: Job, prior: dict) -> str:
    """Runs inside the job-completion transaction. Idempotent: result v1 is created at most once."""
    existing = s.scalar(select(Result).where(Result.job_id == job.id, Result.version == 1))
    reasons = prior["route"]["review_reasons"]
    if existing is None:
        agg = prior["aggregate"]
        warnings = [f"{a['type']} artifact {a['status']}: {a['reason']}" for a in prior["extract"]["artifacts"]
                    if a["status"] in ("failed", "skipped", "partial")]
        warnings += prior["summary"].get("warnings", [])
        s.add(Result(
            job_id=job.id, version=1, status="draft" if reasons else "approved", total_score=agg["total"],
            max_score=agg["max"], breakdown=prior["criteria"]["criteria"], warnings=warnings,
            review_reasons=reasons, created_by="system",
        ))
        s.add(AuditLog(actor_id="system", action="evaluation_finished", target_type="job", target_id=job.id,
                       detail={"needs_review": bool(reasons), "total": agg["total"]}))
    return "needs_review" if reasons else "completed"
