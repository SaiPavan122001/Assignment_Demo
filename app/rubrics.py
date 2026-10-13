"""Versioned, immutable-once-published rubrics."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Rubric

SEED_RUBRIC_NAME = "software-project"

SEED_CRITERIA = [
    {"key": "code_quality", "name": "Code quality",
     "description": "Structure, readability, naming and separation of concerns.", "weight": "0.35", "max_score": "10"},
    {"key": "documentation", "name": "Documentation",
     "description": "README clarity: purpose, setup, usage.", "weight": "0.25", "max_score": "10"},
    {"key": "testing", "name": "Testing",
     "description": "Presence and quality of automated tests.", "weight": "0.25", "max_score": "10"},
    {"key": "functionality", "name": "Functionality",
     "description": "Does the project appear to implement what it claims?", "weight": "0.15", "max_score": "10"},
]


def validate_criteria(criteria: list[dict]) -> None:
    if not criteria:
        raise ValueError("rubric needs at least one criterion")
    keys = [c["key"] for c in criteria]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate criterion keys")
    if sum(Decimal(c["weight"]) for c in criteria) != Decimal("1"):
        raise ValueError("criterion weights must sum to exactly 1")
    for c in criteria:
        if Decimal(c["max_score"]) <= 0:
            raise ValueError(f"max_score must be positive for {c['key']}")


def create_draft(s: Session, name: str, criteria: list[dict]) -> Rubric:
    validate_criteria(criteria)
    latest = s.scalar(select(Rubric.version).where(Rubric.name == name).order_by(Rubric.version.desc()))
    r = Rubric(name=name, version=(latest or 0) + 1, status="draft", criteria=criteria)
    s.add(r)
    s.flush()
    return r


def publish(s: Session, rubric: Rubric) -> Rubric:
    validate_criteria(rubric.criteria)
    rubric.status = "published"
    s.flush()
    return rubric


def get_latest_published(s: Session, name: str = SEED_RUBRIC_NAME) -> Rubric | None:
    return s.scalar(
        select(Rubric).where(Rubric.name == name, Rubric.status == "published").order_by(Rubric.version.desc())
    )


def seed_rubric(s: Session) -> Rubric:
    existing = get_latest_published(s)
    if existing:
        return existing
    return publish(s, create_draft(s, SEED_RUBRIC_NAME, SEED_CRITERIA))
