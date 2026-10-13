"""HTTP routes for the task tracker."""
from flask import Flask, jsonify, request

from app.models import TaskStore

app = Flask(__name__)
store = TaskStore()


@app.get("/tasks")
def list_tasks():
    return jsonify(store.all())


@app.post("/tasks")
def create_task():
    title = (request.get_json(silent=True) or {}).get("title", "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    return jsonify(store.add(title)), 201


if __name__ == "__main__":
    app.run()
