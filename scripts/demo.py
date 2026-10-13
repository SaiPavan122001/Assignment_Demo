"""End-to-end demo: API (in-process FastAPI TestClient) + worker loop + review + publish.

Uses LLM_PROVIDER from the environment (default: mock, fully offline). Run: python tasks.py demo
"""
import io
import json
import logging
import sys
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.auth import seed_users  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models import create_schema  # noqa: E402
from app.pipeline.stages import FINALIZE, STAGES  # noqa: E402
from app.queue.worker import run_once  # noqa: E402
from app.rubrics import seed_rubric  # noqa: E402
from app.runner import zip_directory  # noqa: E402

for noisy in ("httpx", "worker", "llm", "adapters"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
client = TestClient(app)


def step(msg: str) -> None:
    print(f"\n== {msg}")


def login(email: str) -> dict:
    r = client.post("/auth/login", json={"email": email, "password": "password"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def show(label: str, obj) -> None:
    print(f"   {label}: {json.dumps(obj, default=str)}")


def submit(student: dict, title: str, zip_bytes: bytes, notes: str | None = None) -> tuple[str, str]:
    key = str(uuid.uuid4())
    h = {**student, "Idempotency-Key": key}
    a = client.post("/submissions", json={"title": title}, headers=h).json()
    b = client.post("/submissions", json={"title": title}, headers=h).json()
    show("create submission twice, same Idempotency-Key", {"same_id": a["id"] == b["id"]})
    r = client.post(f"/submissions/{a['id']}/artifacts", data={"type": "zip"},
                    files={"file": (f"{title}.zip", zip_bytes)}, headers=student)
    r.raise_for_status()
    if notes:
        client.post(f"/submissions/{a['id']}/artifacts", data={"type": "documentation"},
                    files={"file": ("NOTES.md", notes.encode())}, headers=student).raise_for_status()
    ids = {client.post(f"/submissions/{a['id']}/evaluations", headers=student).json()["id"] for _ in range(5)}
    show("start evaluation x5 -> distinct evaluation ids", len(ids))
    return a["id"], ids.pop()


def drain() -> None:
    while run_once("demo-worker", STAGES, FINALIZE) is not None:
        pass


def main() -> None:
    print(f"LLM provider: {get_settings().llm_provider}  |  database: {get_settings().database_url.split('@')[-1]}")
    engine = db.get_engine()
    create_schema(engine)
    with db.session_scope() as s:
        seed_users(s)
        seed_rubric(s)

    student, other, evaluator = (login(e) for e in ("student1@example.com", "student2@example.com", "evaluator@example.com"))

    step("1. Good project (zip + design notes)")
    sub_id, good = submit(student, "task-tracker", zip_directory(GOLDEN / "good"), "# Design\nSQLite store, Flask routes.")
    show("status before worker", client.get(f"/evaluations/{good}", headers=student).json()["status"])

    step("2. Malicious README project")
    mal_sub, mal = submit(student, "inventory-tool", zip_directory(GOLDEN / "malicious_readme"))

    step("3. Project whose zip is a zip-slip attack (artifact failure must not fail the evaluation)")
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../etc/evil.py", "boom")
    _, bad = submit(student, "bad-zip", evil.getvalue(), "# Notes\nOnly documentation was uploadable.")

    step("4. Worker drains the queue")
    drain()
    for name, job in (("good", good), ("malicious", mal), ("bad-zip", bad)):
        st = client.get(f"/evaluations/{job}", headers=student).json()
        show(name, {"status": st["status"], "stage": st["stage"], "attempts": st["attempts"]})

    step("5. Visibility rules")
    show("student reads unpublished result -> HTTP", client.get(f"/evaluations/{good}/result", headers=student).status_code)
    show("other student reads submission -> HTTP", client.get(f"/submissions/{sub_id}", headers=other).status_code)

    step("6. Evaluator review queue")
    mine = {good: "good", mal: "malicious", bad: "bad-zip"}
    for item in client.get("/review/queue", headers=evaluator).json():
        if item["evaluation_id"] in mine:
            show(mine[item["evaluation_id"]], {"total": item["total_score"], "reasons": item["review_reasons"]})

    step("7. Evaluator publishes good, overrides malicious (injected 100 -> manual scores), publishes both")
    if client.get(f"/evaluations/{good}", headers=evaluator).json()["status"] == "needs_review":
        # e.g. the deterministic 10% audit sample picked it: a human approves before publishing
        client.post(f"/evaluations/{good}/approve", headers=evaluator, json={"note": "audit sample ok"}).raise_for_status()
        print("   (good project was audit-sampled; evaluator approved it first)")
    show("publish good", client.post(f"/evaluations/{good}/publish", headers=evaluator).json()["status"])
    show("publish good again -> HTTP", client.post(f"/evaluations/{good}/publish", headers=evaluator).status_code)
    ov = client.post(f"/evaluations/{mal}/override", headers=evaluator, json={
        "scores": {"code_quality": "4", "documentation": "4", "testing": "1", "functionality": "3"},
        "note": "README attempted prompt injection; regraded manually"}).json()
    show("override -> new version", {"version": ov["version"], "total": ov["total_score"]})
    client.post(f"/evaluations/{mal}/publish", headers=evaluator).raise_for_status()

    step("8. What the student sees")
    res = client.get(f"/evaluations/{good}/result", headers=student).json()
    show("good (published)", {"total": res["total_score"], "criteria": {c["key"]: c["score"] for c in res["breakdown"]}})
    res = client.get(f"/evaluations/{mal}/result", headers=student).json()
    show("malicious (published override)", {"version": res["version"], "total": res["total_score"]})

    step("9. /metrics")
    show("metrics", client.get("/metrics", headers=evaluator).json())
    print("\nDemo finished OK.")


if __name__ == "__main__":
    main()
