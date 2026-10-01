import pytest

from app.models import InvalidTransition


def new_task(db, task_id="t1", **over):
    fields = dict(id=task_id, name="n", repository="/r", worktree="/w", branch="b", base_ref="main",
                  base_sha="abc", prompt="p", status="queued", created_at="2026-01-01T00:00:00Z")
    fields.update(over)
    return db.create_task(**fields)


def test_create_and_get(db):
    task = new_task(db, model="m", reasoning_effort="high", auto_approval=0)
    assert db.get_task("t1") == task
    assert task["status"] == "queued" and task["model"] == "m" and task["auto_approval"] == 0
    assert task["pid"] is None and task["worktree_removed"] == 0
    assert db.get_task("missing") is None


def test_list_newest_first(db):
    new_task(db, "a", created_at="2026-01-01T00:00:00Z")
    new_task(db, "b", created_at="2026-01-02T00:00:00Z")
    assert [t["id"] for t in db.list_tasks()] == ["b", "a"]


def test_update_task(db):
    new_task(db)
    assert db.update_task("t1", git_summary="dirty", pid=42)["pid"] == 42
    with pytest.raises(ValueError):
        db.update_task("t1", bogus=1)
    with pytest.raises(ValueError):
        db.update_task("t1", status="running")  # must go through set_status


def test_set_status_enforces_transitions(db):
    new_task(db)
    assert db.set_status("t1", "starting")["status"] == "starting"
    row = db.set_status("t1", "running", pid=7, started_at="x")
    assert (row["status"], row["pid"], row["started_at"]) == ("running", 7, "x")
    done = db.set_status("t1", "completed", exit_code=0)
    assert done["exit_code"] == 0
    with pytest.raises(InvalidTransition):
        db.set_status("t1", "running")
    assert db.get_task("t1")["status"] == "completed"
    with pytest.raises(KeyError):
        db.set_status("missing", "running")


def test_invalid_create(db):
    with pytest.raises(ValueError):
        new_task(db, status="nope")
    with pytest.raises(ValueError):
        new_task(db, bogus=1)


def test_recent_repos(db):
    db.touch_repo("/a", "2026-01-01T00:00:00Z")
    db.touch_repo("/b", "2026-01-02T00:00:00Z")
    db.touch_repo("/a", "2026-01-03T00:00:00Z")
    assert db.recent_repos() == ["/a", "/b"]


def test_persists_across_connections(settings):
    from app.database import Database
    d1 = Database(settings.db_path)
    new_task(d1, "keep")
    d1.close()
    d2 = Database(settings.db_path)
    assert d2.get_task("keep")["name"] == "n"
    d2.close()
