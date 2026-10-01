import asyncio
import os
import subprocess

import pytest

from app.models import InvalidTransition
from app.task_manager import TaskError

from conftest import wait_for


def go(coro):
    return asyncio.run(coro)


async def create(m, repo, prompt="ok", **kw):
    return await m.create_task(repository=str(repo), prompt=prompt, name=kw.pop("name", "task"), **kw)


async def finished(m, task_id, timeout=15.0):
    return await wait_for(lambda: m.get(task_id)["status"] not in ("queued", "starting", "running")
                          and m.get(task_id), timeout)


def test_create_validation(git_repo, tmp_path, make_manager):
    m = make_manager()

    async def scenario():
        plain = tmp_path / "plain"
        plain.mkdir()
        for kwargs, fragment in [
            (dict(repository=str(git_repo), prompt="  "), "prompt"),
            (dict(repository=str(tmp_path / "nope"), prompt="x"), "not a directory"),
            (dict(repository=str(plain), prompt="x"), "not a git repository"),
            (dict(repository=str(git_repo), prompt="x", base_ref="nope"), "base ref not found"),
            (dict(repository=str(git_repo), prompt="x", reasoning_effort="hi gh;"), "reasoning effort"),
            (dict(repository=str(git_repo), prompt="x", model="--evil"), "model"),
        ]:
            with pytest.raises(TaskError, match=fragment):
                await m.create_task(**kwargs)
        assert m.db.list_tasks() == []  # nothing persisted, no worktrees created
        assert not any(m.settings.worktrees_dir.iterdir())

    go(scenario())


def test_successful_run(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        task = await create(m, git_repo, "ok", name="Fix gossip retry", reasoning_effort="low")
        assert task["status"] == "queued"
        assert task["branch"] == f"codex-gui/{task['id']}-fix-gossip-retry"
        assert os.path.isdir(task["worktree"])
        done = await finished(m, task["id"])
        assert done["status"] == "completed" and done["exit_code"] == 0
        assert done["pid"] and done["started_at"] and done["finished_at"]
        assert done["git_summary"] == "dirty"  # fake codex wrote out.txt
        assert (git_repo.parent / "home" / "worktrees").exists()
        entries, _ = m.read_log(task["id"])
        types = [e["type"] for e in entries]
        assert "thread.started" in types and "raw" in types and "stderr" in types
        assert "item.completed/agent_message" in types and "turn.completed" in types
        assert entries[-1]["message"].startswith("process exited with code 0")
        assert m.db.recent_repos() == [str(git_repo.resolve())]
        info = await m.git_info(task["id"])
        assert info["available"] and "?? out.txt" in info["status"] and "+hello" in info["diff"]

    go(scenario())


def test_failed_run(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "fail", auto_retry=False)
        done = await finished(m, t["id"])
        assert done["status"] == "failed" and done["exit_code"] == 3
        entries, _ = m.read_log(t["id"])
        assert any(e["type"] == "turn.failed" and e["message"] == "boom" for e in entries)

    go(scenario())


def test_spawn_failure_marks_failed(git_repo, settings, db):
    from app.codex_runner import CodexRunner
    from app.task_manager import TaskManager
    m = TaskManager(settings, db, CodexRunner("/definitely/not/codex"))

    async def scenario():
        t = await create(m, git_repo)
        done = await finished(m, t["id"])
        assert done["status"] == "failed" and done["pid"] is None
        assert "failed to start codex" in m.read_log(t["id"])[0][-1]["message"]

    go(scenario())


def test_parallel_execution(git_repo, tmp_path, make_manager):
    """Each task waits for the other one's flag file: only possible if both run at the same time."""
    m = make_manager()
    flags = tmp_path / "flags"
    flags.mkdir()

    async def scenario():
        a = await create(m, git_repo, f"meet A B {flags}", name="a")
        b = await create(m, git_repo, f"meet B A {flags}", name="b")
        c = await create(m, git_repo, "sleep", name="c")  # a third, long-lived one at the same time
        await wait_for(lambda: all(m.get(i)["status"] == "running" for i in (a["id"], b["id"], c["id"])) or
                       (m.get(a["id"])["status"] != "running" and m.get(a["id"])["status"]))
        assert len({m.get(i)["pid"] for i in (a["id"], b["id"], c["id"])}) == 3
        assert (await finished(m, a["id"]))["status"] == "completed"
        assert (await finished(m, b["id"]))["status"] == "completed"
        assert m.get(c["id"])["status"] == "running"
        assert a["worktree"] != b["worktree"] and a["branch"] != b["branch"]
        await m.stop(c["id"])
        await finished(m, c["id"])

    go(scenario())


def test_stop_running_task(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await wait_for(lambda: m.get(t["id"])["status"] == "running")
        pid = m.get(t["id"])["pid"]
        await m.stop(t["id"])
        done = await finished(m, t["id"])
        assert done["status"] == "stopped" and done["exit_code"] == -15
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert os.path.isdir(done["worktree"])  # worktree is never removed automatically
        with pytest.raises(TaskError, match="not active"):
            await m.stop(t["id"])

    go(scenario())


def test_stop_escalates_to_sigkill(git_repo, make_manager):
    m = make_manager(stop_grace_seconds=0.5)

    async def scenario():
        t = await create(m, git_repo, "stubborn")  # ignores SIGTERM
        await wait_for(lambda: m.get(t["id"])["status"] == "running")
        await asyncio.sleep(0.3)  # let the child install its SIGTERM handler
        await m.stop(t["id"])
        done = await finished(m, t["id"])
        assert done["status"] == "stopped" and done["exit_code"] == -9

    go(scenario())


def test_max_concurrent_queues_and_stop_queued(git_repo, make_manager):
    m = make_manager(max_concurrent=1)

    async def scenario():
        first = await create(m, git_repo, "sleep", name="first")
        second = await create(m, git_repo, "ok", name="second")
        third = await create(m, git_repo, "ok", name="third")
        await wait_for(lambda: m.get(first["id"])["status"] == "running")
        await asyncio.sleep(0.3)
        assert m.get(second["id"])["status"] == "queued" and m.get(third["id"])["status"] == "queued"
        stopped = await m.stop(third["id"])  # queued -> stopped without ever starting
        assert stopped["status"] == "stopped" and stopped["pid"] is None
        await m.stop(first["id"])
        assert (await finished(m, second["id"]))["status"] == "completed"
        assert m.get(third["id"])["status"] == "stopped"

    go(scenario())


def test_recover_after_a_gui_restart(git_repo, make_manager, db):
    """Details of the restart recovery are in test_recovery.py; this is the shape: nothing is left "active" without an owner."""
    m = make_manager()
    base = dict(name="n", repository="/r", worktree=str(git_repo), branch="b", base_ref="main", base_sha="a",
                prompt="p", created_at="2026-01-01T00:00:00Z")
    db.create_task(id="r1", status="queued", claimed_by="old-gui", pending_turn='{"prompt": "p"}', **base)
    db.create_task(id="r2", status="running", pid=2 ** 22 + 12345, auto_retry_enabled=0, **base)
    db.create_task(id="r3", status="completed", **base)
    assert sorted(m.recover()) == ["r1", "r2"]
    # r1 was never started: its claim is released so the scheduler starts it. r2's process is gone and auto retry is off.
    assert db.get_task("r1")["status"] == "queued" and db.get_task("r1")["claimed_by"] is None
    assert [db.get_task(i)["status"] for i in ("r2", "r3")] == ["failed", "completed"]
    log = " | ".join(e["message"] for e in m.read_log("r2")[0])
    assert "Codex process is gone" in log and "process_lost" in log
    assert m.recover() == []  # idempotent


def test_shutdown_interrupts_and_kills_children(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await wait_for(lambda: m.get(t["id"])["status"] == "running")
        pid = m.get(t["id"])["pid"]
        await m.shutdown()
        assert m.get(t["id"])["status"] == "interrupted"
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    go(scenario())


def test_commit_and_cleanup_flow(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "ok")
        await finished(m, t["id"])
        wt = t["worktree"]

        # dirty worktree: refused without force, and nothing is deleted
        with pytest.raises(TaskError) as ei:
            await m.delete_worktree(t["id"])
        assert ei.value.code == "dirty" and os.path.isdir(wt)
        with pytest.raises(TaskError, match="worktree first"):
            await m.delete_branch(t["id"])

        await m.commit(t["id"], "add out.txt")
        assert m.get(t["id"])["git_summary"] == "1 commit"
        with pytest.raises(TaskError):
            await m.commit(t["id"], "again")  # nothing left to commit

        row = await m.delete_worktree(t["id"])  # clean now -> no force needed
        assert row["worktree_removed"] == 1 and not os.path.exists(wt)
        branches = subprocess.run(["git", "-C", str(git_repo), "branch", "--list", t["branch"]],
                                  capture_output=True, text=True).stdout
        assert t["branch"] in branches  # branch kept after worktree deletion
        assert (await m.git_info(t["id"]))["available"] is False

        with pytest.raises(TaskError) as ei:
            await m.delete_branch(t["id"])  # has an unmerged commit
        assert ei.value.code == "unmerged"
        assert (await m.delete_branch(t["id"], force=True))["branch_deleted"] == 1

    go(scenario())


def test_force_delete_dirty_worktree(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "ok")
        await finished(m, t["id"])
        row = await m.delete_worktree(t["id"], force=True)
        assert row["worktree_removed"] == 1 and not os.path.exists(t["worktree"])

    go(scenario())


def test_cannot_touch_git_while_active(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await wait_for(lambda: m.get(t["id"])["status"] == "running")
        for op in (m.delete_worktree(t["id"]), m.commit(t["id"], "x"), m.push(t["id"])):
            with pytest.raises(TaskError, match="active"):
                await op
        await m.stop(t["id"])
        await finished(m, t["id"])

    go(scenario())


def test_status_transitions_are_enforced_in_db(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "ok")
        await finished(m, t["id"])
        with pytest.raises(InvalidTransition):
            m.db.set_status(t["id"], "running")

    go(scenario())


def test_many_tasks_in_one_repo_in_parallel(git_repo, tmp_path, make_manager):
    """Created simultaneously in the same repo: separate worktrees/branches, all running at once."""
    m = make_manager()
    flags = tmp_path / "flags"
    flags.mkdir()

    async def scenario():
        names = ["a", "b", "c", "d", "e"]
        # every task waits for every other one's flag (chained pairwise would be weaker): ring of meets
        tasks = await asyncio.gather(*[
            create(m, git_repo, f"meet {n} {names[(i + 1) % len(names)]} {flags}", name="same name")
            for i, n in enumerate(names)])
        assert len({t["worktree"] for t in tasks}) == 5 and len({t["branch"] for t in tasks}) == 5
        for t in tasks:
            assert (await finished(m, t["id"]))["status"] == "completed"
        listing = subprocess.run(["git", "-C", str(git_repo), "worktree", "list"], capture_output=True, text=True).stdout
        assert listing.count("codex-gui/") == 5

    go(scenario())


def test_resume_interrupted_continues_thread_and_skips_sessionless(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        a = await create(m, git_repo, "ok")
        await finished(m, a["id"])
        b = await create(m, git_repo, "ok")
        await finished(m, b["id"])
        thread = m.get(a["id"])["codex_thread_id"]
        assert thread
        m.db._conn.execute("UPDATE tasks SET status='interrupted'")
        m.db.update_task(b["id"], codex_thread_id=None)
        res = await m.resume_interrupted()
        assert res["resumed"] == [a["id"]]
        assert [s["id"] for s in res["skipped"]] == [b["id"]]
        await finished(m, a["id"])
        assert m.get(a["id"])["codex_thread_id"] == thread
        assert m.get(b["id"])["status"] == "interrupted"

    go(scenario())
