"""Task persistence."""
import sqlite3


class TaskStore:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("create table if not exists tasks (id integer primary key, title text, done integer default 0)")

    def add(self, title: str) -> dict:
        cur = self.db.execute("insert into tasks (title) values (?)", (title,))
        return {"id": cur.lastrowid, "title": title, "done": False}

    def all(self) -> list[dict]:
        rows = self.db.execute("select id, title, done from tasks").fetchall()
        return [{"id": r[0], "title": r[1], "done": bool(r[2])} for r in rows]
