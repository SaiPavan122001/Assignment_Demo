"""SQLAlchemy models. The three-layer idempotency lives here as DB constraints:
  - idempotency_keys primary key         (layer 1)
  - uq_active_job partial unique index   (layer 2)
  - uq_one_published_result              (publish safety)
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    role: Mapped[str] = mapped_column(String(20))  # student | evaluator | admin
    password_hash: Mapped[str] = mapped_column(String(300))


class Rubric(Base):
    __tablename__ = "rubrics"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published
    # [{key, name, description, weight("0.30"), max_score("10")}]
    criteria: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(100), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    type: Mapped[str] = mapped_column(String(30))  # github|zip|pdf|documentation|live_url|google_drive|video
    source_ref: Mapped[str | None] = mapped_column(Text)  # URL or original filename
    storage_key: Mapped[str | None] = mapped_column(Text)  # uploaded file location in Storage
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # e.g. {"transcript_key": ...}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Job(Base):
    """An evaluation job. `id` doubles as the evaluation id exposed by the API."""
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "uq_active_job", "submission_id", "rubric_id", unique=True,
            postgresql_where=text("status in ('queued','running')"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    rubric_id: Mapped[str] = mapped_column(ForeignKey("rubrics.id"))
    # queued | running | needs_review | completed | failed | dead
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic lock
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str | None] = mapped_column(String(30))  # last finished stage
    checkpoints: Mapped[dict] = mapped_column(JSONB, default=dict)  # stage -> output
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Outbox(Base):
    """Written in the same transaction as the job; a dispatcher (here: the worker claim) marks it sent."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    topic: Mapped[str] = mapped_column(String(50), default="evaluation.requested")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Result(Base):
    __tablename__ = "results"
    __table_args__ = (
        UniqueConstraint("job_id", "version"),
        Index("uq_one_published_result", "job_id", unique=True, postgresql_where=text("status = 'published'")),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    # draft | approved | published | rejected | superseded
    status: Mapped[str] = mapped_column(String(20), default="draft")
    total_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    max_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    breakdown: Mapped[list] = mapped_column(JSONB, default=list)
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    review_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    created_by: Mapped[str] = mapped_column(String(36), default="system")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(50))
    target_type: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(36))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    purpose: Mapped[str] = mapped_column(String(100))  # e.g. "criterion:testing", "summary:file"
    provider: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LLMCache(Base):
    __tablename__ = "llm_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(prompt + rubric_version)
    response: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)


# Postgres trigger: a published rubric can never be changed or deleted.
_IMMUTABLE_RUBRIC_DDL = [
    """
create or replace function rubric_immutable() returns trigger as $$
begin
  if old.status = 'published' then
    raise exception 'published rubric % v% is immutable', old.name, old.version;
  end if;
  if tg_op = 'DELETE' then return old; end if;
  return new;
end $$ language plpgsql
""",
    "drop trigger if exists trg_rubric_immutable on rubrics",
    """
create trigger trg_rubric_immutable before update or delete on rubrics
  for each row execute function rubric_immutable()
""",
]


def create_schema(engine, drop_first: bool = False) -> None:
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        for stmt in _IMMUTABLE_RUBRIC_DDL:
            c.execute(text(stmt))
