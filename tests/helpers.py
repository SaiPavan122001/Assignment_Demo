def make_submission(api, email="student1@example.com", key="k1", title="My project"):
    r = api.c.post("/submissions", json={"title": title}, headers=api.h(email, **{"Idempotency-Key": key}))
    assert r.status_code == 201, r.text
    return r.json()


def add_readme_artifact(api, sub_id, email="student1@example.com"):
    r = api.c.post(
        f"/submissions/{sub_id}/artifacts", data={"type": "documentation"},
        files={"file": ("README.md", b"# Hello\n", "text/markdown")}, headers=api.h(email),
    )
    assert r.status_code == 201, r.text
    return r.json()
