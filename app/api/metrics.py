from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import STAFF, require_roles
from app.db import get_session
from app.models import Job, LLMCall, User

router = APIRouter(tags=["ops"])


@router.get("/metrics")
def metrics(user: User = Depends(require_roles(*STAFF)), s: Session = Depends(get_session)):
    by_status = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    oldest = s.scalar(select(func.min(Job.created_at)).where(Job.status == "queued"))
    age = (datetime.now(timezone.utc) - oldest).total_seconds() if oldest else 0.0
    live = LLMCall.cache_hit.is_(False)
    calls, hits, tin, tout, saved, cost = s.execute(
        select(func.count(), func.count().filter(LLMCall.cache_hit),
               func.coalesce(func.sum(LLMCall.tokens_in).filter(live), 0),
               func.coalesce(func.sum(LLMCall.tokens_out).filter(live), 0),
               func.coalesce(func.sum(LLMCall.tokens_in + LLMCall.tokens_out).filter(LLMCall.cache_hit), 0),
               func.coalesce(func.sum(LLMCall.cost_usd), 0))
    ).one()
    return {
        "queue_depth": by_status.get("queued", 0),
        "running": by_status.get("running", 0),
        "dead_letter": by_status.get("dead", 0),
        "jobs_by_status": by_status,
        "oldest_queued_age_seconds": round(age, 1),
        "llm": {"calls": calls, "cache_hits": hits, "tokens_in": int(tin), "tokens_out": int(tout), "tokens_saved_by_cache": int(saved),
                "estimated_cost_usd": str(cost)},
    }
