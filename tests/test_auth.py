from tests.helpers import add_readme_artifact, make_submission


def test_requires_token(client):
    assert client.get("/submissions").status_code == 401


def test_bad_login(client):
    r = client.post("/auth/login", json={"email": "student1@example.com", "password": "nope"})
    assert r.status_code == 401


def test_student_cannot_read_another_students_submission(api):
    sub = make_submission(api, "student1@example.com")
    r = api.c.get(f"/submissions/{sub['id']}", headers=api.h("student2@example.com"))
    assert r.status_code == 404
    # owner and evaluator can
    assert api.c.get(f"/submissions/{sub['id']}", headers=api.h("student1@example.com")).status_code == 200
    assert api.c.get(f"/submissions/{sub['id']}", headers=api.h("evaluator@example.com")).status_code == 200


def test_student_cannot_upload_to_or_evaluate_another_students_submission(api):
    sub = make_submission(api, "student1@example.com")
    add_readme_artifact(api, sub["id"])
    r = api.c.post(f"/submissions/{sub['id']}/evaluations", headers=api.h("student2@example.com"))
    assert r.status_code == 404
    r = api.c.post(
        f"/submissions/{sub['id']}/artifacts", data={"type": "github", "url": "https://github.com/a/b"},
        headers=api.h("student2@example.com"),
    )
    assert r.status_code == 404


def test_listing_only_shows_own_submissions(api):
    make_submission(api, "student1@example.com", key="a")
    make_submission(api, "student2@example.com", key="b")
    mine = api.c.get("/submissions", headers=api.h("student1@example.com")).json()
    assert len(mine) == 1
    assert len(api.c.get("/submissions", headers=api.h("evaluator@example.com")).json()) == 2
