import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import db
from app.config import get_settings


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
    yield eng
    eng.dispose()
