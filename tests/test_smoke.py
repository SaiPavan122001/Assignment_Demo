from sqlalchemy import text


def test_database_reachable(engine):
    with engine.connect() as c:
        assert c.execute(text("select 1")).scalar() == 1
        assert "PostgreSQL" in c.execute(text("select version()")).scalar()
