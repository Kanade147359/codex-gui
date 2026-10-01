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


def test_codex_thread_columns(db):
    task = new_task(db)
    assert task["codex_thread_id"] is None and task["last_turn_at"] is None
    row = db.update_task("t1", codex_thread_id="abc", last_turn_at="2026-01-01T00:00:00Z")
    assert row["codex_thread_id"] == "abc" and row["last_turn_at"] == "2026-01-01T00:00:00Z"


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    from app.database import Database
    from app.database import Database
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, name TEXT NOT NULL, repository TEXT NOT NULL, worktree TEXT NOT NULL,
            branch TEXT NOT NULL, base_ref TEXT NOT NULL, base_sha TEXT NOT NULL, prompt TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '', reasoning_effort TEXT NOT NULL DEFAULT 'default',
            auto_approval INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, pid INTEGER, exit_code INTEGER,
            git_summary TEXT NOT NULL DEFAULT '', worktree_removed INTEGER NOT NULL DEFAULT 0,
            branch_deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
        INSERT INTO tasks (id, name, repository, worktree, branch, base_ref, base_sha, prompt, status, created_at)
            VALUES ('old', 'n', '/r', '/w', 'b', 'main', 'a', 'p', 'completed', '2026-01-01T00:00:00Z');
    """)
    old.commit()
    old.close()
    d = Database(path)
    task = d.get_task("old")
    assert task["codex_thread_id"] is None and task["last_turn_at"] is None
    d.update_task("old", codex_thread_id="x")
    d.close()
    Database(path).close()  # opening an already migrated database is fine


def turn(db, task_id="t1", n=1, **over):
    fields = dict(task_id=task_id, turn=n, session=1, thread_id="th", created_at="2026-01-01T00:00:0%dZ" % n,
                  input_tokens=100, cached_input_tokens=80, output_tokens=10, total_json="{}")
    fields.update(over)
    return db.add_turn(**fields)


def test_turns_roundtrip_and_latest(db):
    new_task(db, "t1")
    new_task(db, "t2")
    assert db.list_turns("t1") == [] and db.latest_turns() == {}
    row = turn(db, "t1", 1)
    assert row["cache_write_input_tokens"] is None and row["reasoning_output_tokens"] is None
    turn(db, "t1", 2, input_tokens=200, reasoning_output_tokens=7)
    turn(db, "t2", 1)
    assert [t["turn"] for t in db.list_turns("t1")] == [1, 2]
    latest = db.latest_turns()
    assert latest["t1"]["turn"] == 2 and latest["t1"]["input_tokens"] == 200 and latest["t2"]["turn"] == 1
    with pytest.raises(Exception):
        turn(db, "t1", 2)  # (task, turn) is unique
    with pytest.raises(ValueError):
        db.add_turn(task_id="t1", bogus=1)


def test_old_database_gets_the_new_columns_and_keeps_its_data(tmp_path):
    import sqlite3
    from app.database import Database
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, name TEXT NOT NULL, repository TEXT NOT NULL, worktree TEXT NOT NULL,
            branch TEXT NOT NULL, base_ref TEXT NOT NULL, base_sha TEXT NOT NULL, prompt TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '', reasoning_effort TEXT NOT NULL DEFAULT 'default',
            auto_approval INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, pid INTEGER, exit_code INTEGER,
            git_summary TEXT NOT NULL DEFAULT '', worktree_removed INTEGER NOT NULL DEFAULT 0,
            branch_deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            codex_thread_id TEXT, last_turn_at TEXT);
        CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, turn INTEGER NOT NULL,
            session INTEGER NOT NULL, thread_id TEXT NOT NULL, created_at TEXT NOT NULL, input_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_write_input_tokens INTEGER,
            reasoning_output_tokens INTEGER, total_json TEXT NOT NULL, UNIQUE (task_id, turn));
        INSERT INTO tasks VALUES ('a1','old','/r','/w','b','main','sha','p','m','high',1,'completed',NULL,0,'',0,0,'t0',NULL,NULL,'thr-1',NULL);
        INSERT INTO turns (task_id, turn, session, thread_id, created_at, input_tokens, cached_input_tokens, output_tokens, total_json)
            VALUES ('a1', 1, 1, 'thr-1', 't1', 10, 5, 2, '{}');
    """)
    conn.commit()
    conn.close()
    d = Database(path)
    t = d.get_task("a1")
    assert t["name"] == "old" and t["codex_thread_id"] == "thr-1"
    # the optimization defaults apply to old rows: Standard, low verbosity, no web search, sandboxed
    assert (t["service_tier"], t["model_verbosity"], t["web_search_enabled"], t["sandbox"]) == ("default", "low", 0, "workspace-write")
    assert (t["adaptive_reasoning"], t["context_guard"], t["latest_input_tokens"], t["five_hour_used_before"]) == (1, 1, None, None)
    old_turn = d.list_turns("a1")[0]
    assert old_turn["kind"] == "turn" and old_turn["status"] == "completed" and old_turn["cache_hit_rate"] is None
    d.close()
    Database(path).close()  # opening it again does not try to add the columns twice


def test_rate_limit_history_roundtrip(db):
    db.add_rate_limits(ts="t1", reason="turn_start", task_id="a", primary_used_percent=10, primary_window_mins=300,
                       secondary_used_percent=3, secondary_window_mins=10080, ordinary_usage_allowed=1)
    db.add_rate_limits(ts="t2", reason="update", primary_used_percent=11)
    rows = db.list_rate_limits()
    assert [r["ts"] for r in rows] == ["t2", "t1"]  # newest first
    assert rows[1]["secondary_used_percent"] == 3 and rows[1]["task_id"] == "a"
    assert [r["ts"] for r in db.list_rate_limits(task_id="a")] == ["t1"]
    with pytest.raises(ValueError):
        db.add_rate_limits(ts="t", reason="r", bogus=1)
