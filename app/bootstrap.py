"""Create tables, seed users and the rubric. Idempotent: `python -m app.bootstrap`."""
from app import db
from app.auth import seed_users
from app.models import create_schema
from app.rubrics import seed_rubric


def main() -> None:
    engine = db.get_engine()
    create_schema(engine)
    with db.session_scope() as s:
        seed_users(s)
        seed_rubric(s)
    print("schema ready; users and rubric seeded")


if __name__ == "__main__":
    main()
