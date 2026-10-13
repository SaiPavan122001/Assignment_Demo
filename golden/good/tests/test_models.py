from app.models import TaskStore


def test_add_and_list():
    store = TaskStore()
    store.add("write tests")
    assert store.all()[0]["title"] == "write tests"


def test_new_task_is_not_done():
    assert TaskStore().add("x")["done"] is False
