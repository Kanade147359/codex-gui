"""1 task = 1 worktree = 1 Codex session: thread id capture, resume, usage per turn."""
import asyncio
import json

import pytest

from app.task_manager import TaskError

from conftest import wait_for


def go(coro):
    return asyncio.run(coro)


async def create(m, repo, prompt="ok", **kw):
    return await m.create_task(repository=str(repo), prompt=prompt, name=kw.pop("name", "task"), **kw)


async def finished(m, task_id, timeout=15.0):
    return await wait_for(lambda: m.get(task_id)["status"] not in ("queued", "starting", "running")
                          and m.get(task_id), timeout)


def invocations(state):
    return [json.loads(line) for line in (state / "invocations.jsonl").read_text().splitlines()]


def messages(m, task_id):
    return [e["message"] for e in m.read_log(task_id)[0] if e["stream"] == "system"]


def test_thread_id_is_saved_from_the_first_run(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo)
        done = await finished(m, t["id"])
        assert done["codex_thread_id"].startswith("thread-")
        assert done["last_turn_at"]
        assert any(msg == f"codex session id: {done['codex_thread_id']}" for msg in messages(m, t["id"]))

    go(scenario())


def test_missing_thread_id_is_logged(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "nothread")
        done = await finished(m, t["id"])
        assert done["status"] == "completed" and done["codex_thread_id"] is None
        assert any("no thread.started event" in msg and "Start New Session" in msg for msg in messages(m, t["id"]))
        with pytest.raises(TaskError) as ei:
            await m.send_instruction(t["id"], "more")
        assert ei.value.status == 409 and ei.value.code == "no_session"
        # starting a new session is still possible, and it does not need a resumable thread
        await m.start_new_session(t["id"], "ok")
        assert (await wait_for(lambda: m.get(t["id"])["codex_thread_id"] and m.get(t["id"])))["codex_thread_id"]

    go(scenario())


def test_three_turns_resume_the_same_thread_in_the_same_worktree(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "ok first")
        thread = (await finished(m, t["id"]))["codex_thread_id"]
        for prompt in ("ok second", "ok third"):
            row = await m.send_instruction(t["id"], prompt)
            assert row["status"] == "queued" and row["pid"] is None and row["exit_code"] is None
            done = await wait_for(lambda: m.get(t["id"])["status"] == "completed" and
                                  len(m.db.list_turns(t["id"])) == 2 + (prompt == "ok third") and m.get(t["id"]))
            assert done["codex_thread_id"] == thread  # never replaced: same session

        runs = invocations(fake_codex_state)
        assert [r["argv"] for r in runs] == [[], ["resume", thread], ["resume", thread]]
        assert {r["cwd"] for r in runs} == {t["worktree"]}  # all turns ran in the task's one worktree
        assert [r["prompt"] for r in runs] == ["ok first", "ok second", "ok third"]  # raw prompts, no history stuffed in

        turns = m.usage(t["id"])["turns"]
        assert [(r["turn"], r["session"], r["thread_id"]) for r in turns] == [(1, 1, thread), (2, 1, thread), (3, 1, thread)]
        # the CLI reports thread totals (100/0, 200/80, 300/160 ...); the table shows each turn's own usage
        assert [(r["input_tokens"], r["cached_input_tokens"], r["output_tokens"]) for r in turns] == [
            (100, 0, 10), (100, 80, 10), (100, 80, 10)]
        assert [r["cache_hit_rate"] for r in turns] == [0, 80, 80]
        assert [r["uncached_input_tokens"] for r in turns] == [100, 20, 20]
        assert m.usage(t["id"])["latest"]["turn"] == 3

    go(scenario())


def test_dashboard_cache_rate_is_the_latest_turns(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo)
        await finished(m, t["id"])
        assert [x["cache_hit_rate"] for x in m.list_tasks_view()] == [0]
        await m.send_instruction(t["id"], "ok")
        await wait_for(lambda: len(m.db.list_turns(t["id"])) == 2 and m.get(t["id"])["status"] == "completed")
        assert [x["cache_hit_rate"] for x in m.list_tasks_view()] == [80]
        fresh = await create(m, git_repo, "sleep", name="no turn yet")
        assert {x["id"]: x["cache_hit_rate"] for x in m.list_tasks_view()}[fresh["id"]] is None
        await m.stop(fresh["id"])
        await finished(m, fresh["id"])

    go(scenario())


def test_zero_input_is_not_displayed(git_repo, make_manager):
    m = make_manager()
    m.db.create_task(id="z", name="n", repository="/r", worktree="/w", branch="b", base_ref="main", base_sha="a",
                     prompt="p", status="completed", created_at="2026-01-01T00:00:00Z")
    m.db.add_turn(task_id="z", turn=1, session=1, thread_id="th", created_at="x", input_tokens=0,
                  cached_input_tokens=0, output_tokens=3, total_json="{}")
    assert m.usage("z")["latest"]["cache_hit_rate"] is None
    assert m.list_tasks_view()[0]["cache_hit_rate"] is None


def test_new_session_is_a_separate_explicit_action(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo)
        first = (await finished(m, t["id"]))["codex_thread_id"]
        await m.start_new_session(t["id"], "ok again")
        second = (await wait_for(lambda: m.get(t["id"])["codex_thread_id"] != first and m.get(t["id"])))["codex_thread_id"]
        await finished(m, t["id"])
        assert second != first
        assert invocations(fake_codex_state)[1]["argv"] == []  # fresh `codex exec`, not resume
        assert invocations(fake_codex_state)[1]["cwd"] == t["worktree"]  # ... but in the same worktree
        # a new thread starts its own usage baseline and gets its own session number
        turns = m.usage(t["id"])["turns"]
        assert [(r["turn"], r["session"], r["thread_id"]) for r in turns] == [(1, 1, first), (2, 2, second)]
        assert [r["input_tokens"] for r in turns] == [100, 100] and turns[1]["cached_input_tokens"] == 0
        # and Send now continues the NEW session
        await m.send_instruction(t["id"], "ok")
        await wait_for(lambda: len(m.db.list_turns(t["id"])) == 3 and m.get(t["id"])["status"] == "completed")
        assert invocations(fake_codex_state)[2]["argv"] == ["resume", second]

    go(scenario())


def test_send_is_refused_while_the_task_runs(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await wait_for(lambda: m.get(t["id"])["status"] == "running")
        for op in (m.send_instruction(t["id"], "x"), m.start_new_session(t["id"], "x")):
            with pytest.raises(TaskError) as ei:
                await op
            assert ei.value.status == 409 and ei.value.code == "active"
        assert len(invocations(fake_codex_state)) == 1  # no second codex process was started
        await m.stop(t["id"])
        await finished(m, t["id"])

    go(scenario())


def test_stopped_and_failed_tasks_can_resume(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await wait_for(lambda: m.get(t["id"])["codex_thread_id"])
        thread = m.get(t["id"])["codex_thread_id"]
        await wait_for(lambda: m.get(t["id"])["status"] == "running")
        await m.stop(t["id"])
        assert (await finished(m, t["id"]))["status"] == "stopped"
        await m.send_instruction(t["id"], "ok")
        done = await wait_for(lambda: m.get(t["id"])["status"] == "completed" and m.get(t["id"]))
        assert done["codex_thread_id"] == thread and done["exit_code"] == 0
        assert invocations(fake_codex_state)[1]["argv"] == ["resume", thread]

    go(scenario())


def test_resume_that_lands_on_another_thread_is_flagged(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo)
        thread = (await finished(m, t["id"]))["codex_thread_id"]
        await m.send_instruction(t["id"], "newthread")
        done = await wait_for(lambda: m.get(t["id"])["status"] == "completed" and
                              len(m.db.list_turns(t["id"])) == 2 and m.get(t["id"]))
        assert done["codex_thread_id"] == thread  # the recorded session is not silently replaced
        assert any(msg.startswith("WARNING: asked to resume") for msg in messages(m, t["id"]))
        assert m.usage(t["id"])["turns"][1]["session"] == 2  # its usage is not mixed into session 1

    go(scenario())


def test_instruction_validation_and_missing_worktree(git_repo, make_manager):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo)
        await finished(m, t["id"])
        for op in (m.send_instruction, m.start_new_session):
            with pytest.raises(TaskError, match="required"):
                await op(t["id"], "   ")
            with pytest.raises(TaskError) as ei:
                await op("missing", "x")
            assert ei.value.status == 404
        await m.delete_worktree(t["id"], force=True)
        with pytest.raises(TaskError) as ei:
            await m.send_instruction(t["id"], "x")
        assert ei.value.code == "no_worktree"

    go(scenario())


def test_instruction_is_written_to_the_log_but_history_is_not_replayed(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "ok first prompt")
        await finished(m, t["id"])
        await m.send_instruction(t["id"], "ok second prompt")
        await wait_for(lambda: len(m.db.list_turns(t["id"])) == 2 and m.get(t["id"])["status"] == "completed")
        assert any(msg == "instruction:\nok second prompt" for msg in messages(m, t["id"]))
        # what codex received is exactly what the user typed
        assert invocations(fake_codex_state)[1]["prompt"] == "ok second prompt"

    go(scenario())
