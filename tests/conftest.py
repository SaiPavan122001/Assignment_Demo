import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import db
from app.config import get_settings
from app.models import Base, create_schema


def _ensure_database(url: str) -> None:
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        exists = c.execute(text("select 1 from pg_database where datname=:n"), {"n": u.database}).scalar()
        if not exists:
            c.execute(text(f'create database "{u.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine():
    url = get_settings().test_database_url
    _ensure_database(url)
    eng = db.init_engine(url)
    create_schema(eng, drop_first=True)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_db(engine):
    """Every test starts with empty tables."""
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as c:
        c.execute(text(f"truncate {names} restart identity cascade"))
    yield
