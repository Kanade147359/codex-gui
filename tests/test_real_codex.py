"""Opt-in check against the real codex CLI: CODEX_GUI_REAL=1 pytest tests/test_real_codex.py -s

It spends a few tokens (CODEX_GUI_REAL_PAUSE=<seconds> waits between turns). Same thread across turns is the REQUIRED outcome; cached input is only printed,
because prompt caching is best-effort and must not make the test flaky.
"""
import asyncio
import os
from pathlib import Path

import pytest

from app.codex_runner import CodexRunner
from app.task_manager import TaskManager

from conftest import wait_for

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_REAL") != "1", reason="set CODEX_GUI_REAL=1 to use the real codex")

PROMPTS = [
    "Reply with exactly the word: one. Do not run any commands.",
    "Reply with exactly the word: two. Do not run any commands.",
    "Reply with exactly the word: three. Do not run any commands.",
]


def test_three_turns_resume_one_real_session(git_repo, settings, db):
    m = TaskManager(settings, db, CodexRunner(os.environ.get("CODEX_BIN", "codex")))

    async def scenario():
        t = await m.create_task(repository=str(git_repo), prompt=PROMPTS[0], name="real", auto_approval=True)

        def idle(n):
            row = m.get(t["id"])
            return row if row["status"] not in ("queued", "starting", "running") and len(m.db.list_turns(t["id"])) >= n else None

        row = await wait_for(lambda: idle(1), timeout=240)
        thread = row["codex_thread_id"]
        assert row["status"] == "completed" and thread
        for n, prompt in enumerate(PROMPTS[1:], start=2):
            # The cache needs a moment to become usable; CODEX_GUI_REAL_PAUSE=<seconds> spaces the turns out.
            await asyncio.sleep(float(os.environ.get("CODEX_GUI_REAL_PAUSE", "0")))
            await m.send_instruction(t["id"], prompt)
            row = await wait_for(lambda: idle(n), timeout=240)
            assert row["status"] == "completed"
            assert row["codex_thread_id"] == thread

        turns = m.usage(t["id"])["turns"]
        print(f"\nthread {thread}\nTURN  INPUT  CACHED  UNCACHED  HIT  OUTPUT")
        for r in turns:
            hit = "-" if r["cache_hit_rate"] is None else f"{r['cache_hit_rate']:.1f}%"
            print(f"{r['turn']:>4} {r['input_tokens']:>6} {r['cached_input_tokens']:>7} {r['uncached_input_tokens']:>9} {hit:>5} {r['output_tokens']:>7}")
        assert [r["thread_id"] for r in turns] == [thread] * 3  # every turn reported the same session
        assert [r["session"] for r in turns] == [1, 1, 1]
        log = [e["message"] for e in m.read_log(t["id"])[0] if e["stream"] == "system"]
        assert not any(msg.startswith("WARNING") for msg in log)

    asyncio.run(scenario())


def test_three_turns_one_real_app_server_thread(git_repo, settings, db):
    """The app-server path with the real codex: one thread for every turn, usage and limits readable.

    Required: the same thread id each turn, usage stored per turn, the thread's context window known.
    Printed only: cache hit (best-effort) and the rate-limit snapshot.
    """
    settings.backend = "app-server"
    m = TaskManager(settings, db)  # the real CodexRunner / AppServerClient
    model = os.environ.get("CODEX_GUI_REAL_MODEL", "gpt-6.1-sol")

    async def scenario():
        t = await m.create_task(repository=str(git_repo), prompt=PROMPTS[0], name="real-app-server", model=model,
                                reasoning_effort="low")

        def idle(n):
            row = m.get(t["id"])
            return row if row["status"] not in ("queued", "starting", "running") and len(m.db.list_turns(t["id"])) >= n else None

        row = await wait_for(lambda: idle(1), timeout=300)
        assert row["status"] == "completed", row["status_detail"]
        thread = row["codex_thread_id"]
        for n, prompt in enumerate(PROMPTS[1:], start=2):
            await asyncio.sleep(float(os.environ.get("CODEX_GUI_REAL_PAUSE", "0")))
            await m.send_instruction(t["id"], prompt)
            row = await wait_for(lambda: idle(n), timeout=300)
            assert row["status"] == "completed", row["status_detail"]
            assert row["codex_thread_id"] == thread

        view = m.present_task(m.get(t["id"]))
        turns = m.usage(t["id"])["turns"]
        print(f"\nmodel {view['effective_model']}  thread {thread}\nTURN  INPUT  CACHED  UNCACHED  HIT  OUTPUT")
        for r in turns:
            hit = "-" if r["cache_hit_rate"] is None else f"{r['cache_hit_rate']:.1f}%"
            print(f"{r['turn']:>4} {r['input_tokens']:>6} {r['cached_input_tokens']:>7} {r['uncached_input_tokens']:>9} {hit:>5} {r['output_tokens']:>7}")
        print("context", view["context"])
        print("observed quota", view["observed_quota"])
        limits = await m.rate_limits(force=True)
        print("limits", [(w["label"], w["used_percent"], w["resets_at"]) for w in limits.get("windows", [])],
              "resets", limits.get("available_resets"))
        assert [r["thread_id"] for r in turns] == [thread] * 3 and [r["session"] for r in turns] == [1, 1, 1]
        assert all(r["input_tokens"] > 0 and r["output_tokens"] > 0 for r in turns)
        assert view["context"]["window"] and view["context"]["tokens"]
        assert limits["available"]
        log = [e["message"] for e in m.read_log(t["id"])[0] if e["stream"] == "system"]
        assert not any(msg.startswith("WARNING") for msg in log)
        await m.shutdown()

    asyncio.run(scenario())


# ---------- automatic recovery with the real codex ----------

RECOVERY_PROMPT_TASK = ("Run exactly this shell command and wait for it: `sleep 20; echo recovered > marker.txt`. "
                        "Then reply with the single word: done. Do not run any other command.")


def _command_running(worktree: str) -> bool:
    """The model's long shell command is running in this task's worktree right now (its command line is the only place the
    text appears; the cwd check keeps a stray process of another run or session from counting)."""
    import subprocess
    pids = subprocess.run(["pgrep", "-f", "echo recovered > marker.txt"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        try:
            if os.path.realpath(f"/proc/{pid}/cwd").startswith(os.path.realpath(worktree)):
                return True
        except OSError:
            pass
    return False


async def _terminate_mid_turn(m, task_id):
    """SIGKILL the real codex process of a running turn (the exec process, or the shared app-server) while the model's
    long shell command is running, so that the turn is really in the middle of its work."""
    import signal
    await wait_for(lambda: m.get(task_id)["status"] == "running" and m.get(task_id)["codex_thread_id"], timeout=180)
    await wait_for(lambda: _command_running(m.get(task_id)["worktree"]), timeout=180, interval=0.2)
    pid = m.get(task_id)["pid"]
    assert pid, "no process id recorded"
    os.kill(pid, signal.SIGKILL)
    return pid


@pytest.mark.parametrize("backend", ["exec", "app-server"])
def test_real_codex_killed_mid_turn_is_recovered_in_the_same_thread_and_worktree(git_repo, settings, db, backend):
    """Opt-in. The real codex is SIGKILLed in the middle of a turn; the GUI must retry in the SAME worktree and the SAME
    Codex thread (no new session), and the work must finish. Prints the evidence (thread ids, cache reuse)."""
    settings.backend = backend
    settings.retry_backoff_seconds = (2.0,)
    m = TaskManager(settings, db)  # the real CodexRunner / AppServerClient
    model = os.environ.get("CODEX_GUI_REAL_MODEL", "gpt-6.1-sol")

    async def scenario():
        m.scheduler.start(0.2)
        t = await m.create_task(repository=str(git_repo), prompt=RECOVERY_PROMPT_TASK, name=f"real-recovery-{backend}",
                                model=model, reasoning_effort="low")
        worktree = t["worktree"]
        killed_pid = await _terminate_mid_turn(m, t["id"])
        waiting = await wait_for(lambda: m.get(t["id"])["status"] in ("retry_wait", "queued", "starting", "running", "completed") and m.get(t["id"]), timeout=60)
        thread = m.get(t["id"])["codex_thread_id"]
        done = await wait_for(lambda: m.get(t["id"])["status"] in ("completed", "failed") and m.get(t["id"]), timeout=300)
        await m.scheduler.stop()
        attempts = m.attempts(t["id"])
        print(f"\nbackend {backend}: killed pid {killed_pid}; thread before {thread}, after {done['codex_thread_id']}")
        print(f"worktree before {worktree}, after {done['worktree']}")
        print("ATTEMPT  TRIGGER      RESULT        RESUMED  THREAD")
        for a in attempts:
            print(f"{a['attempt_number']:>7}  {a['trigger_kind']:<12} {a['result']:<13} {a['was_resume']:>7}  {a['codex_thread_id']}")
        turns = m.usage(t["id"])["turns"]
        for r in turns:
            hit = "-" if r["cache_hit_rate"] is None else f"{r['cache_hit_rate']:.1f}%"
            print(f"turn {r['turn']} thread {r['thread_id']} input {r['input_tokens']} cached {r['cached_input_tokens']} hit {hit}")
        assert done["status"] == "completed", (done["status"], done["status_detail"])
        assert done["codex_thread_id"] == thread and thread                   # the same Codex thread
        assert done["worktree"] == worktree and (Path(worktree) / "marker.txt").read_text().strip() == "recovered"
        assert [a["result"] for a in attempts][-1] == "completed" and attempts[0]["result"] == "interrupted"
        assert attempts[-1]["was_resume"] == 1 and {a["codex_thread_id"] for a in attempts} == {thread}
        log = "\n".join(e["message"] for e in m.read_log(t["id"])[0] if e["stream"] == "system")
        assert "WARNING" not in log and "Retry started a new Codex thread" not in log
        await m.shutdown()

    asyncio.run(scenario())
