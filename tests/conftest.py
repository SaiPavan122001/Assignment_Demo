import os
import tempfile

os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="eval-uploads-"))
os.environ["APP_ENV"] = "test"  # SSRF_ALLOW_PRIVATE is honored only in this mode

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


@pytest.fixture
def seeded(engine):
    from app.auth import seed_users
    from app.db import session_scope
    from app.rubrics import seed_rubric
    with session_scope() as s:
        seed_users(s)
        r = seed_rubric(s)
        return {"rubric_id": r.id}


@pytest.fixture
def client(seeded):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


class Api:
    """Tiny helper: logs in as a seeded user and returns auth headers."""
    def __init__(self, client):
        self.c = client
        self._tokens = {}

    def h(self, email: str, **extra) -> dict:
        if email not in self._tokens:
            r = self.c.post("/auth/login", json={"email": email, "password": "password"})
            assert r.status_code == 200, r.text
            self._tokens[email] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._tokens[email]}", **extra}


@pytest.fixture
def api(client):
    return Api(client)

@pytest.fixture(autouse=True)
def no_random_audit(monkeypatch):
    """Random audit sampling is deterministic per job id; switch it off so tests are not order-dependent."""
    from app.pipeline import routing
    monkeypatch.setattr(routing, "AUDIT_PERCENT", 0)
