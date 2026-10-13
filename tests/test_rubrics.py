import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import rubrics
from app.db import session_scope
from app.models import Rubric


def test_seed_rubric_is_published_and_weights_sum_to_one(engine):
    with session_scope() as s:
        r = rubrics.seed_rubric(s)
        assert r.status == "published" and r.version == 1
    with session_scope() as s:
        assert rubrics.seed_rubric(s).version == 1  # seeding twice does not create v2


def test_published_rubric_is_immutable_at_db_level(engine):
    with session_scope() as s:
        rid = rubrics.seed_rubric(s).id
    with pytest.raises(DBAPIError, match="immutable"):
        with session_scope() as s:
            s.execute(text("update rubrics set name='hacked' where id=:i"), {"i": rid})
    with pytest.raises(DBAPIError, match="immutable"):
        with session_scope() as s:
            s.execute(text("delete from rubrics where id=:i"), {"i": rid})


def test_new_version_is_a_new_row(engine):
    with session_scope() as s:
        v1 = rubrics.seed_rubric(s)
        c = [dict(x) for x in v1.criteria]
        c[0]["weight"], c[1]["weight"] = "0.30", "0.30"
        v2 = rubrics.publish(s, rubrics.create_draft(s, v1.name, c))
        assert v2.version == 2
    with session_scope() as s:
        assert len(s.scalars(select(Rubric)).all()) == 2


def test_bad_weights_rejected(engine):
    with session_scope() as s:
        with pytest.raises(ValueError, match="sum to exactly 1"):
            rubrics.create_draft(s, "x", [{"key": "a", "name": "A", "description": "", "weight": "0.9", "max_score": "10"}])
