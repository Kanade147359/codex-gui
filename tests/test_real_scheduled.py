"""Opt-in check of a scheduled instruction against the REAL codex:

    CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_scheduled.py -s

Task X runs turn 1; task A runs; an instruction for X is scheduled "after A"; when A has completed it is sent to X's EXISTING
Codex thread as turn 2. The required outcome is the same thread id (and session, worktree, branch) for both turns. It spends a
few tokens per backend (three tiny turns each). Cached input is printed only: prompt caching is best-effort.
"""
import asyncio
import os

import pytest

from app.codex_runner import CodexRunner
from app.task_manager import TaskManager

from conftest import wait_for

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_REAL") != "1", reason="set CODEX_GUI_REAL=1 to use the real codex")

ONE = "Reply with exactly the word: one. Do not run any commands."
A = "Reply with exactly the word: alpha. Do not run any commands."
TWO = "Reply with exactly the word: two. Do not run any commands."


@pytest.mark.parametrize("backend", ["exec", "app-server"])
def test_scheduled_instruction_is_turn_two_of_the_same_real_thread(git_repo, settings, db, backend):
    settings.backend = backend
    m = TaskManager(settings, db, CodexRunner(os.environ.get("CODEX_BIN", "codex")))

    def settled(task_id):
        row = m.get(task_id)
        return row if row["status"] not in ("queued", "starting", "running") else None

    async def scenario():
        m.scheduler.start(0.5)                            # what the GUI's lifespan does: nothing sends a scheduled instruction without it
        x = await m.create_task(repository=str(git_repo), prompt=ONE, name="X", auto_approval=True)
        row = await wait_for(lambda: settled(x["id"]), timeout=240)
        thread = row["codex_thread_id"]
        assert row["status"] == "completed" and thread
        worktree, branch = row["worktree"], row["branch"]
        assert len(m.db.list_turns(x["id"])) == 1

        a = await m.create_task(repository=str(git_repo), prompt=A, name="A", auto_approval=True)
        sched = await m.schedule_instruction(x["id"], TWO, [a["id"]], "standard")
        first_state = sched["status"]                     # A has only just been started: normally still waiting for it
        done = await wait_for(lambda: (m.db.get_scheduled(sched["id"]) or {}).get("status") in ("completed", "failed", "blocked") and
                              m.db.get_scheduled(sched["id"]), timeout=480)
        assert done["status"] == "completed", done
        assert m.get(a["id"])["status"] == "completed"
        row = await wait_for(lambda: settled(x["id"]), timeout=240)

        turns = m.usage(x["id"])["turns"]
        print(f"\n[{backend}] thread {thread}   instruction #{sched['id']} first seen as {first_state}\nTURN  INPUT  CACHED  UNCACHED  HIT  OUTPUT")
        for r in turns:
            hit = "-" if r["cache_hit_rate"] is None else f"{r['cache_hit_rate']:.1f}%"
            print(f"{r['turn']:>4} {r['input_tokens']:>6} {r['cached_input_tokens']:>7} {r['uncached_input_tokens']:>9} {hit:>5} {r['output_tokens']:>7}  tier={r['service_tier']}")
        assert len(turns) == 2
        assert [r["thread_id"] for r in turns] == [thread, thread]       # turn 2 is on the same Codex thread as turn 1
        assert [r["session"] for r in turns] == [1, 1]                   # no new session
        assert row["codex_thread_id"] == thread and (row["worktree"], row["branch"]) == (worktree, branch)
        attempts = m.attempts(x["id"])
        assert [a_["trigger_kind"] for a_ in attempts] == ["initial", "scheduled_instruction"], attempts
        assert attempts[1]["was_resume"] and attempts[1]["codex_thread_id"] == thread
        replies = [(e.get("event") or {}).get("item", {}).get("text") or e["message"] for e in m.read_log(x["id"])[0]
                   if e["type"].endswith("agentMessage") or ((e.get("event") or {}).get("item") or {}).get("type") in ("agent_message", "agentMessage")]
        assert replies and "two" in replies[-1].lower(), replies                  # the last reply is the answer to the scheduled instruction
        await m.shutdown()

    asyncio.run(scenario())
