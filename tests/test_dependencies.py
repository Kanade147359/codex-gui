"""Task dependencies: the DAG in the database, and the manager starting dependents exactly once."""
import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app.database import Database, DependencyError
from app.task_manager import TaskError

from conftest import wait_for


def go(coro):
    return asyncio.run(coro)


def row(db, task_id, **over):
    fields = dict(id=task_id, name=task_id.upper(), repository="/r", worktree="/w", branch="b", base_ref="main",
                  base_sha="abc", prompt="p", status="completed", task_outcome="success", created_at="2026-01-01T00:00:00Z")
    fields.update(over)
    return db.create_task(**fields)


async def create(m, repo, prompt="ok", **kw):
    return await m.create_task(repository=str(repo), prompt=prompt, name=kw.pop("name", "task"), **kw)


def status(m, task_id):
    return m.get(task_id)["status"]


async def reaches(m, task_id, wanted, timeout=15.0):
    return await wait_for(lambda: status(m, task_id) in wanted and m.get(task_id), timeout)


def invocations(state):
    path = Path(state) / "invocations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ---------- the DAG ----------

def test_self_dependency_is_rejected(db):
    row(db, "a")
    with pytest.raises(DependencyError) as e:
        db.add_dependencies("a", ["a"])
    assert e.value.code == "self"
    with pytest.raises(sqlite3.IntegrityError):  # the table refuses it on its own as well
        db._conn.execute("INSERT INTO task_dependencies (task_id, depends_on_task_id, created_at) VALUES ('a', 'a', 'x')")


def test_duplicate_dependency_is_rejected(db):
    row(db, "a"), row(db, "b")
    with pytest.raises(DependencyError) as e:
        db.add_dependencies("a", ["b", "b"])
    assert e.value.code == "duplicate"
    db.add_dependencies("a", ["b"])
    with pytest.raises(DependencyError) as e:  # the same edge again
        db.add_dependencies("a", ["b"])
    assert e.value.code == "duplicate"
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE (task_id, depends_on_task_id)
        db._conn.execute("INSERT INTO task_dependencies (task_id, depends_on_task_id, created_at) VALUES ('a', 'b', 'x')")
    assert db.dependencies_of("a") == ["b"]


def test_cycles_are_rejected_however_long(db):
    for t in "abcd":
        row(db, t)
    db.add_dependencies("a", ["b"])      # A -> B
    db.add_dependencies("b", ["c"])      # B -> C
    with pytest.raises(DependencyError) as e:
        db.add_dependencies("c", ["a"])  # C -> A closes the loop
    assert e.value.code == "cycle" and "c -> a -> b -> c" in str(e.value)
    with pytest.raises(DependencyError):
        db.add_dependencies("c", ["b"])  # a two-task loop
    assert db.dependencies_of("c") == []  # nothing was written
    db.add_dependencies("d", ["a", "b", "c"])  # a diamond / fan-in is not a cycle
    db.add_dependencies("a", ["c"])            # neither is a second path to the same task


def test_unknown_dependency_is_rejected(db):
    row(db, "a")
    with pytest.raises(DependencyError) as e:
        db.add_dependencies("a", ["nope"])
    assert e.value.code == "missing"


def test_replace_dependencies_is_all_or_nothing(db):
    for t in "abc":
        row(db, t)
    db.add_dependencies("a", ["b"])
    db.add_dependencies("b", ["c"])
    with pytest.raises(DependencyError):
        db.replace_dependencies("c", ["a"])  # would close C -> A -> B -> C
    db.replace_dependencies("a", ["c"])      # the old edge A -> B no longer counts, and goes away
    assert db.dependencies_of("a") == ["c"]
    with pytest.raises(DependencyError):
        db.replace_dependencies("a", ["c", "c"])
    assert db.dependencies_of("a") == ["c"]  # the failed replace left the old edges alone


def test_a_task_is_created_together_with_its_edges_or_not_at_all(db):
    row(db, "a")
    with pytest.raises(DependencyError):
        row(db, "d", status="waiting_dependencies", depends_on=["a", "a"])
    assert db.get_task("d") is None
    d = row(db, "d", status="waiting_dependencies", depends_on=["a"])
    assert d["status"] == "waiting_dependencies" and db.dependencies_of("d") == ["a"] and db.dependents_of("a") == ["d"]


# ---------- atomic steps ----------

def test_queue_if_ready_needs_every_prerequisite_completed(db):
    row(db, "a"), row(db, "b", status="running")
    row(db, "d", status="waiting_dependencies", depends_on=["a", "b"])
    assert not db.queue_if_ready("d") and db.get_task("d")["status"] == "waiting_dependencies"
    db.set_status("b", "completed")
    assert db.queue_if_ready("d") and db.get_task("d")["status"] == "queued"
    assert not db.queue_if_ready("d")  # already moved on: a second evaluator gets nothing


def test_block_if_failed_only_when_a_prerequisite_failed(db):
    row(db, "a", status="running"), row(db, "b", status="running")
    row(db, "d", status="waiting_dependencies", depends_on=["a", "b"])
    assert not db.block_if_failed("d", "x")
    db.set_status("a", "stopped")
    assert db.block_if_failed("d", "Dependency A was stopped")
    assert db.get_task("d")["status"] == "blocked" and db.get_task("d")["status_detail"] == "Dependency A was stopped"


def test_paused_prerequisites_do_not_block(db):
    """waiting-for-quota and interrupted are resumable pauses, not failures: the dependent keeps waiting."""
    row(db, "a", status="waiting-for-quota"), row(db, "b", status="interrupted")
    row(db, "d", status="waiting_dependencies", depends_on=["a", "b"])
    assert not db.block_if_failed("d", "x") and not db.queue_if_ready("d")


def test_concurrent_evaluators_queue_a_task_once(tmp_path):
    """Several connections (as several server processes would have) race to start the same task: one wins."""
    path = tmp_path / "shared.db"
    first = Database(path)
    row(first, "a"), row(first, "b")
    row(first, "d", status="waiting_dependencies", depends_on=["a", "b"])
    dbs = [first] + [Database(path) for _ in range(5)]
    wins, barrier = [], threading.Barrier(len(dbs) * 2)

    def evaluate(database):
        barrier.wait()
        wins.append(database.queue_if_ready("d"))

    threads = [threading.Thread(target=evaluate, args=(d,)) for d in dbs for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1 and len(wins) == 12
    # ... and the same for the claim that follows
    claims = []
    barrier2 = threading.Barrier(len(dbs))

    def claim(i, database):
        barrier2.wait()
        claims.append(database.claim_queued("d", f"owner-{i}"))

    threads = [threading.Thread(target=claim, args=(i, d)) for i, d in enumerate(dbs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert claims.count(True) == 1
    for d in dbs[1:]:
        d.close()


def test_set_status_does_not_overwrite_a_concurrent_change(db):
    row(db, "a", status="running")
    stale = db.get_task("a")
    db.set_status("a", "stopped")
    # a writer that decided "running -> completed" from a stale read must lose
    assert not db.transition("a", stale["status"], "completed")
    assert db.get_task("a")["status"] == "stopped"


# ---------- creation ----------

def test_create_validates_dependencies(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "sleep")
        with pytest.raises(TaskError, match="not found"):
            await create(m, git_repo, depends_on=["nope"])
        with pytest.raises(TaskError, match="duplicate"):
            await create(m, git_repo, depends_on=[a["id"], a["id"]])
        with pytest.raises(TaskError, match="policy"):
            await create(m, git_repo, depends_on=[a["id"]], dependency_policy="whenever")
        assert len(m.db.list_tasks()) == 1  # nothing was created by the refused calls
        await m.stop(a["id"])

    go(scenario())


def test_a_dependent_task_has_no_worktree_until_it_runs(git_repo, make_manager, tmp_path):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "sleep")
        d = await create(m, git_repo, "ok D", depends_on=[a["id"]], name="D")
        assert d["status"] == "waiting_dependencies" and d["worktree_pending"] == 1
        assert not Path(d["worktree"]).exists()
        listing = __import__("subprocess").run(["git", "-C", str(git_repo), "worktree", "list"], capture_output=True, text=True).stdout
        assert d["branch"] not in listing and a["branch"] in listing
        await m.stop(a["id"])

    go(scenario())


# ---------- running dependents ----------

def test_dependent_waits_for_the_prerequisite_then_runs_once(git_repo, make_manager, tmp_path, fake_codex_state):
    m = make_manager()
    gate = tmp_path / "gate-a"

    async def scenario():
        a = await create(m, git_repo, f"gate {gate}", name="A")
        b = await create(m, git_repo, "ok B", depends_on=[a["id"]], name="B")
        await asyncio.sleep(0.5)
        assert status(m, a["id"]) == "running" and status(m, b["id"]) == "waiting_dependencies"
        assert not [i for i in invocations(fake_codex_state) if i["prompt"] == "ok B"]  # B has not been started
        gate.write_text("open")
        done = await reaches(m, b["id"], {"completed"})
        assert status(m, a["id"]) == "completed"
        assert len([i for i in invocations(fake_codex_state) if i["prompt"] == "ok B"]) == 1
        assert done["worktree_pending"] == 0 and Path(done["worktree"]).is_dir()  # created just before it ran
        assert [x["trigger_kind"] for x in m.db.list_attempts(b["id"])] == ["initial"]

    go(scenario())


def test_fan_in_starts_the_dependent_exactly_once(git_repo, make_manager, tmp_path, fake_codex_state):
    m = make_manager()
    gates = [tmp_path / f"gate-{n}" for n in "abc"]

    async def scenario():
        parents = [await create(m, git_repo, f"gate {g}", name=n) for g, n in zip(gates, "ABC")]
        d = await create(m, git_repo, "ok D", depends_on=[p["id"] for p in parents], name="D")
        for g in gates[:2]:
            g.write_text("open")
        await reaches(m, parents[0]["id"], {"completed"}), await reaches(m, parents[1]["id"], {"completed"})
        await asyncio.sleep(0.3)
        assert status(m, d["id"]) == "waiting_dependencies"  # two of three: still waiting
        view = next(t for t in m.list_tasks_view() if t["id"] == d["id"])
        assert (view["deps_done"], view["deps_total"]) == (2, 3)
        gates[2].write_text("open")
        await reaches(m, d["id"], {"completed"})
        # several evaluators are around (listener per parent, scheduler ticks): still one start
        for _ in range(5):
            m.tick()
            m._evaluate_dependents(parents[0]["id"])
        await asyncio.sleep(0.3)
        assert len([i for i in invocations(fake_codex_state) if i["prompt"] == "ok D"]) == 1
        assert len(m.db.list_attempts(d["id"])) == 1

    go(scenario())


def test_simultaneous_parent_completion_does_not_double_start(git_repo, make_manager, fake_codex_state):
    """Both parents finish in the same instant and every evaluator (listener, tick, explicit) runs: one start."""
    m = make_manager()

    async def scenario():
        base = dict(repository=str(git_repo), worktree=str(git_repo), branch="b", base_ref="main", base_sha="a", prompt="p",
                    created_at="2026-01-01T00:00:00Z", status="running")
        m.db.create_task(id="p1", name="P1", **base)
        m.db.create_task(id="p2", name="P2", **base)
        d = await create(m, git_repo, "ok D", depends_on=["p1", "p2"], name="D")
        assert d["status"] == "waiting_dependencies"
        m.db.set_status("p1", "completed", task_outcome="success")
        m.db.set_status("p2", "completed", task_outcome="success")
        for _ in range(10):  # a flood of redundant evaluations
            m.tick()
            m._evaluate_dependents("p1")
            m._evaluate_dependents("p2")
            m._evaluate_one(d["id"])
        await reaches(m, d["id"], {"completed"})
        await asyncio.sleep(0.3)
        assert len([i for i in invocations(fake_codex_state) if i["prompt"] == "ok D"]) == 1
        assert len(m.db.list_attempts(d["id"])) == 1

    go(scenario())


def test_the_scheduler_alone_starts_a_ready_task(git_repo, make_manager):
    """No listener: only the background loop looks at the database."""
    m = make_manager()

    async def scenario():
        m.detach()
        a = await create(m, git_repo, "ok", name="A")
        await reaches(m, a["id"], {"completed"})
        d = await create(m, git_repo, "ok D", depends_on=[a["id"]], name="D")
        assert d["status"] in ("waiting_dependencies", "queued")  # evaluated at creation; nobody starts it without the loop
        m.scheduler.start(0.05)
        await reaches(m, d["id"], {"completed"})
        await m.scheduler.stop()

    go(scenario())


def test_dependent_is_queued_at_creation_when_the_prerequisite_is_already_done(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "ok", name="A")
        await reaches(m, a["id"], {"completed"})
        d = await create(m, git_repo, "ok D", depends_on=[a["id"]], name="D")
        await reaches(m, d["id"], {"completed"})

    go(scenario())


# ---------- failures ----------

def test_failed_prerequisite_blocks_the_dependent_and_nothing_runs(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "err 1 something is wrong", name="A", auto_retry=False)
        d = await create(m, git_repo, "ok D", depends_on=[a["id"]], name="D")
        await reaches(m, a["id"], {"failed"})
        blocked = await reaches(m, d["id"], {"blocked"})
        assert blocked["status_detail"] == "Dependency A failed"
        await asyncio.sleep(0.3)
        assert not [i for i in invocations(fake_codex_state) if i["prompt"] == "ok D"]
        assert not Path(blocked["worktree"]).exists()  # never given a worktree

    go(scenario())


def test_stopping_a_prerequisite_blocks_dependents_down_the_chain(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "sleep", name="A")
        b = await create(m, git_repo, "ok", depends_on=[a["id"]], name="B")
        c = await create(m, git_repo, "ok", depends_on=[b["id"]], name="C")
        await reaches(m, a["id"], {"running"})
        await m.stop(a["id"])
        await reaches(m, a["id"], {"stopped"})
        assert (await reaches(m, b["id"], {"blocked"}))["status_detail"] == "Dependency A was stopped"
        assert (await reaches(m, c["id"], {"blocked"}))["status_detail"] == "Dependency B is blocked"

    go(scenario())


def test_prerequisite_in_retry_wait_keeps_the_dependent_waiting_until_it_recovers(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(1.0,))

    async def scenario():
        a = await create(m, git_repo, "crash", name="A")        # dies once, then recovers on retry
        b = await create(m, git_repo, "ok B", depends_on=[a["id"]], name="B")
        await reaches(m, a["id"], {"retry_wait"})
        for _ in range(5):
            m.tick()
        assert status(m, b["id"]) == "waiting_dependencies"  # a temporary failure never blocks
        m.scheduler.start(0.05)
        await reaches(m, a["id"], {"completed"})
        await reaches(m, b["id"], {"completed"})
        await m.scheduler.stop()
        assert m.get(a["id"])["retry_count"] == 1

    go(scenario())


def test_dependent_is_blocked_only_when_the_prerequisite_finally_fails(git_repo, make_manager, monkeypatch):
    m = make_manager(retry_backoff_seconds=(0.2,))
    monkeypatch.setenv("FAKE_CODEX_FORCE_MODE", "crashloop")

    async def scenario():
        a = await create(m, git_repo, "ok", name="A", max_retries=2)
        b = await create(m, git_repo, "ok B", depends_on=[a["id"]], name="B")
        m.scheduler.start(0.05)
        await reaches(m, a["id"], {"retry_wait"})
        assert status(m, b["id"]) == "waiting_dependencies"
        await reaches(m, a["id"], {"failed"})
        assert m.get(a["id"])["retry_count"] == 2
        assert (await reaches(m, b["id"], {"blocked"}))["status_detail"] == "Dependency A failed"
        await m.scheduler.stop()

    go(scenario())


# ---------- the buttons: Run Anyway, Retry Failed Dependency ----------

def test_run_anyway_starts_a_blocked_task(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "err 1 nope", name="A", auto_retry=False)
        d = await create(m, git_repo, "ok D", depends_on=[a["id"]], name="D")
        await reaches(m, d["id"], {"blocked"})
        with pytest.raises(TaskError):
            await m.run_anyway(a["id"])  # only waiting or blocked tasks
        await m.run_anyway(d["id"])
        done = await reaches(m, d["id"], {"completed"})
        assert done["worktree_pending"] == 0
        assert status(m, a["id"]) == "failed"  # the prerequisite itself is untouched
        assert len([i for i in invocations(fake_codex_state) if i["prompt"] == "ok D"]) == 1

    go(scenario())


def test_retry_failed_dependency_resumes_the_chain(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "err 1 nope", name="A", auto_retry=False)
        b = await create(m, git_repo, "ok B", depends_on=[a["id"]], name="B")
        c = await create(m, git_repo, "ok C", depends_on=[b["id"]], name="C")
        await reaches(m, c["id"], {"blocked"})
        res = await m.retry_failed_dependencies(c["id"])
        assert set(res["retried"]) == {a["id"], b["id"]}
        await reaches(m, a["id"], {"completed"})  # the retry resumed A's own thread with the recovery instruction
        await reaches(m, b["id"], {"completed"})
        await reaches(m, c["id"], {"completed"})

    go(scenario())


def test_stopping_a_waiting_or_blocked_task(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "sleep", name="A")
        d = await create(m, git_repo, "ok", depends_on=[a["id"]], name="D")
        stopped = await m.stop(d["id"])
        assert stopped["status"] == "stopped"
        e = await create(m, git_repo, "ok", depends_on=[d["id"]], name="E")
        assert (await reaches(m, e["id"], {"blocked"}))["status_detail"] == "Dependency D was stopped"
        await m.stop(a["id"])

    go(scenario())


def test_set_dependencies_refuses_a_cycle_and_a_started_task(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "sleep", name="A")
        b = await create(m, git_repo, "ok", depends_on=[a["id"]], name="B")
        c = await create(m, git_repo, "ok", depends_on=[b["id"]], name="C")
        with pytest.raises(TaskError) as e:
            await m.set_dependencies(b["id"], [c["id"]])  # B -> C -> B
        assert e.value.code == "cycle"
        with pytest.raises(TaskError) as e:
            await m.set_dependencies(b["id"], [b["id"]])
        assert e.value.code == "self"
        with pytest.raises(TaskError, match="before it starts"):
            await m.set_dependencies(a["id"], [b["id"]])  # A is running already
        assert m.db.dependencies_of(b["id"]) == [a["id"]]  # unchanged
        await m.set_dependencies(c["id"], [])  # no prerequisites left: it can start
        await reaches(m, c["id"], {"completed"})
        await m.stop(a["id"])

    go(scenario())


# ---------- HTTP ----------

def test_dependencies_over_http(git_repo, settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from conftest import FakeRunner
    with TestClient(create_app(settings, FakeRunner())) as c:
        a = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "sleep", "name": "A"}).json()
        r = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "name": "D", "depends_on": [a["id"]]})
        d = r.json()
        assert r.status_code == 200 and d["status"] == "waiting_dependencies"
        assert c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "depends_on": ["nope"]}).status_code == 400
        assert c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "depends_on": [a["id"], a["id"]]}).status_code == 400
        shown = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}[d["id"]]
        assert shown["dependencies"] == [{"id": a["id"], "name": "A", "status": shown["dependencies"][0]["status"],
            "task_outcome": "needs_review", "outcome_reason": "Completion has not been checked."}]
        assert (shown["deps_done"], shown["deps_total"]) == (0, 1)
        detail = c.get(f"/api/tasks/{d['id']}").json()
        assert detail["dependencies"][0]["id"] == a["id"] and "pending_turn" not in detail
        assert c.get(f"/api/tasks/{a['id']}").json()["dependents"][0]["id"] == d["id"]
        assert c.put(f"/api/tasks/{d['id']}/dependencies", json={"depends_on": [d["id"]]}).status_code == 400
        assert c.post(f"/api/tasks/{d['id']}/stop").json()["status"] == "stopped"
        c.post(f"/api/tasks/{a['id']}/stop")
        import time
        for _ in range(100):  # let the stopped process end before the app shuts down
            if c.get(f"/api/tasks/{a['id']}").json()["status"] == "stopped":
                break
            time.sleep(0.05)


# ---------- status model and old databases ----------

@pytest.mark.parametrize("old,new,ok", [
    ("waiting_dependencies", "queued", True), ("waiting_dependencies", "blocked", True), ("waiting_dependencies", "stopped", True),
    ("blocked", "queued", True), ("blocked", "waiting_dependencies", True), ("blocked", "stopped", True),
    ("running", "retry_wait", True), ("starting", "retry_wait", True),
    ("retry_wait", "queued", True), ("retry_wait", "stopped", True), ("retry_wait", "failed", True),
    ("waiting_dependencies", "running", False), ("waiting_dependencies", "starting", False), ("blocked", "running", False),
    ("retry_wait", "running", False), ("retry_wait", "completed", False), ("completed", "retry_wait", False),
    ("queued", "retry_wait", False), ("queued", "waiting_dependencies", True), ("starting", "waiting_dependencies", True),
])
def test_new_status_transitions(old, new, ok):
    from app.models import can_transition
    assert can_transition(old, new) is ok


def test_an_old_database_is_migrated_and_keeps_its_tasks(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, name TEXT NOT NULL, repository TEXT NOT NULL, worktree TEXT NOT NULL,
        branch TEXT NOT NULL, base_ref TEXT NOT NULL, base_sha TEXT NOT NULL, prompt TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
        reasoning_effort TEXT NOT NULL DEFAULT 'default', auto_approval INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL, pid INTEGER,
        exit_code INTEGER, git_summary TEXT NOT NULL DEFAULT '', worktree_removed INTEGER NOT NULL DEFAULT 0,
        branch_deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)""")
    conn.execute("INSERT INTO tasks (id, name, repository, worktree, branch, base_ref, base_sha, prompt, status, created_at) "
                 "VALUES ('old1', 'n', '/r', '/w', 'b', 'main', 'abc', 'p', 'completed', '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    db = Database(path)
    task = db.get_task("old1")
    assert task["auto_retry_enabled"] == 1 and task["max_retries"] == 3 and task["retry_count"] == 0
    assert task["dependency_policy"] == "all_success" and task["worktree_pending"] == 0 and task["pending_turn"] is None
    row(db, "d", status="waiting_dependencies", depends_on=["old1"])  # the new tables are there too
    assert task["status"] == "completed" and task["task_outcome"] == "success"
    assert task["outcome_source"] == "legacy"
    assert db.queue_if_ready("d")
    db.close()


def test_a_queued_task_from_before_the_upgrade_is_not_started_blindly(git_repo, make_manager, db):
    """Without a recorded pending turn nobody knows whether it was a first run or a continuation: the user decides."""
    m = make_manager()
    row(db, "legacy", status="queued", worktree=str(git_repo))
    row(db, "modern", status="queued", worktree=str(git_repo), pending_turn='{"prompt": "p"}', claimed_by="old-gui")
    assert sorted(m.recover()) == ["legacy", "modern"]
    assert db.get_task("legacy")["status"] == "interrupted"
    assert db.get_task("modern")["status"] == "queued" and db.get_task("modern")["claimed_by"] is None
