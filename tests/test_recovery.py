"""Automatic recovery: what is retried and what is not, how, and that the same worktree and Codex thread are kept."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import recovery
from app.appserver import AppServerError
from app.procinfo import process_identity, same_process
from app.recovery import NON_RETRYABLE, QUOTA, RETRYABLE, UNKNOWN, RECOVERY_PROMPT, NO_THREAD_NOTE
from app.task_manager import TaskError

from conftest import wait_for

HERE = Path(__file__).parent


def go(coro):
    return asyncio.run(coro)


async def create(m, repo, prompt="ok", **kw):
    return await m.create_task(repository=str(repo), prompt=prompt, name=kw.pop("name", "task"), **kw)


def status(m, task_id):
    return m.get(task_id)["status"]


async def reaches(m, task_id, wanted, timeout=15.0):
    return await wait_for(lambda: status(m, task_id) in wanted and m.get(task_id), timeout)


def invocations(state):
    path = Path(state) / "invocations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def log_text(m, task_id):
    return "\n".join(e["message"] for e in m.read_log(task_id)[0])


def seconds(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------- classification ----------

@pytest.mark.parametrize("code,detail,kind,category", [
    (-9, "", "process_exited", RETRYABLE),                    # killed by a signal
    (137, "", "process_exited", RETRYABLE),
    (1, "stream disconnected before completion", "connection", RETRYABLE),
    (1, "error sending request for url (https://x)", "connection", RETRYABLE),
    (1, "Resource temporarily unavailable (EAGAIN)", "transient_io", RETRYABLE),
    (1, "internal error in codex", "internal_error", RETRYABLE),
    (1, "401 Unauthorized", "auth", NON_RETRYABLE),
    (1, "You are not logged in. Run `codex login`", "auth", NON_RETRYABLE),
    (1, "You've hit your usage limit", "quota", QUOTA),
    (1, "429 Too Many Requests", "quota", QUOTA),
    (2, "error: unexpected argument '--bogus' found", "args", NON_RETRYABLE),
    (1, "invalid model: gpt-nope", "model", NON_RETRYABLE),
    (1, "Error loading config.toml: invalid type", "config", NON_RETRYABLE),
    (3, "boom", "unknown", UNKNOWN),
    (1, "", "unknown", UNKNOWN),
])
def test_exec_failures_are_classified(code, detail, kind, category):
    f = recovery.classify_exit(code, detail)
    assert (f.kind, f.category) == (kind, category)


@pytest.mark.parametrize("info,kind,category", [
    ("httpConnectionFailed", "connection", RETRYABLE), ("responseStreamDisconnected", "connection", RETRYABLE),
    ("serverOverloaded", "server_overloaded", RETRYABLE), ("internalServerError", "internal_error", RETRYABLE),
    ("usageLimitExceeded", "quota", QUOTA), ("rateLimitExceeded", "quota", QUOTA),
    ("unauthorized", "auth", NON_RETRYABLE), ("badRequest", "bad_request", NON_RETRYABLE),
    ("contextWindowExceeded", "context_window", NON_RETRYABLE), ("sandboxError", "sandbox", NON_RETRYABLE),
    ("other", "unknown", UNKNOWN), ("", "unknown", UNKNOWN),
])
def test_app_server_turn_errors_are_classified(info, kind, category):
    f = recovery.classify_turn_error(info, "something")
    assert (f.kind, f.category) == (kind, category)


def test_the_structured_error_wins_over_the_text():
    # the message mentions a timeout, but Codex said the credentials are bad: not worth retrying
    assert recovery.classify_turn_error("unauthorized", "request timed out").category == NON_RETRYABLE


@pytest.mark.parametrize("error,kind,category", [
    (AppServerError("codex app-server exited: x", kind="closed"), "app_server_exited", RETRYABLE),
    (AppServerError("turn/start timed out after 60s", kind="timeout"), "app_server_timeout", RETRYABLE),
    (AppServerError("cannot start codex app-server: [Errno 2]", kind="spawn"), "codex_unavailable", NON_RETRYABLE),
    (AppServerError("no rollout found for thread id abc", -32000, "rpc"), "session_missing", NON_RETRYABLE),
    (AppServerError("Invalid params", -32602, "rpc"), "bad_request", NON_RETRYABLE),
    (AppServerError("something odd", -32000, "rpc"), "unknown", UNKNOWN),
])
def test_app_server_errors_are_classified(error, kind, category):
    f = recovery.classify_app_server_error(error)
    assert (f.kind, f.category) == (kind, category)


def test_git_worktree_errors():
    assert recovery.classify_git_error("fatal: Unable to create '/r/.git/index.lock': File exists.").category == RETRYABLE
    assert recovery.classify_git_error("fatal: a branch named 'x' already exists").category == NON_RETRYABLE


@pytest.mark.parametrize("category,enabled,count,limit,action,reason", [
    (RETRYABLE, True, 0, 3, "retry", "x"), (UNKNOWN, True, 2, 3, "retry", "x"),   # unknown is retried within the limit
    (RETRYABLE, True, 3, 3, "fail", "retry_limit"), (UNKNOWN, True, 3, 3, "fail", "retry_limit"),
    (RETRYABLE, False, 0, 3, "fail", "auto_retry_disabled"), (RETRYABLE, True, 0, 0, "fail", "retry_limit"),
    (NON_RETRYABLE, True, 0, 3, "fail", "x"), (QUOTA, True, 0, 3, "quota", "quota"),
])
def test_decision(category, enabled, count, limit, action, reason):
    d = recovery.decide(recovery.Failure("x", category, "m"), enabled=enabled, retry_count=count, max_retries=limit)
    assert d.action == action and (d.reason == reason or reason == "x")


def test_backoff_schedule():
    schedule = (10.0, 30.0, 60.0)
    assert [recovery.backoff_seconds(schedule, n) for n in (1, 2, 3, 4, 9)] == [10, 30, 60, 60, 60]
    assert recovery.backoff_seconds((), 1) == 0


def test_the_recovery_instruction_is_fixed_and_does_not_repeat_the_original_work():
    assert RECOVERY_PROMPT.startswith("Previous turn was interrupted unexpectedly.")
    for needle in ("Inspect the current worktree", "Do not redo work that is already complete", "git status", "git log",
                   "never create a commit or push that already exists", "Run the relevant checks"):
        assert needle in RECOVERY_PROMPT


# ---------- process identity (pid reuse) ----------

def test_a_pid_is_only_trusted_with_the_same_start_time_and_command_line():
    me = os.getpid()
    identity = process_identity(me)
    assert identity and identity == process_identity(me)               # stable for one process
    assert same_process(me, identity, needle="python") or same_process(me, identity, needle="pytest")
    assert not same_process(me, identity.rsplit(":", 1)[0] + ":1", needle="python")  # another start time: a reused pid
    assert not same_process(me, "other-boot:" + identity.rsplit(":", 1)[1], needle="python")  # another boot
    assert not same_process(me, identity, needle="definitely-not-in-the-command-line")
    assert not same_process(None, identity) and not same_process(2 ** 22 + 7, identity)
    assert process_identity(2 ** 22 + 7) is None


def test_an_ended_process_is_not_the_same_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass  # codex"])
    identity = process_identity(proc.pid)
    proc.wait()
    assert process_identity(proc.pid) is None and not same_process(proc.pid, identity)


# ---------- retrying an unexpected stop (exec backend) ----------

def test_unexpected_exit_is_retried_in_the_same_worktree_and_thread(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.3,))

    async def scenario():
        t = await create(m, git_repo, "crash", name="net")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        thread = waiting["codex_thread_id"]
        assert thread and waiting["retry_count"] == 1 and waiting["max_retries"] == 3
        assert waiting["last_failure_kind"] == "process_exited" and waiting["last_exit_code"] == -9
        assert waiting["status_detail"].startswith("Retry 1/3 in 0.3s")
        assert seconds(waiting["next_retry_at"]) - time.time() <= 0.31  # (upper bound only: a slow machine may look late)
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"completed"})

        first, second = invocations(fake_codex_state)
        assert first["argv"][:1] != ["resume"] and first["prompt"] == "crash"
        assert second["argv"] == ["resume", thread]                       # the SAME Codex thread, resumed
        assert second["prompt"] == RECOVERY_PROMPT                        # not the original instruction again
        assert first["cwd"] == second["cwd"] == done["worktree"]          # the SAME worktree
        assert done["codex_thread_id"] == thread and done["branch"] == t["branch"]
        assert "Retry started a new Codex thread" not in log_text(m, t["id"])
        await m.scheduler.stop()

    go(scenario())


def test_retry_history(git_repo, make_manager):
    m = make_manager(retry_backoff_seconds=(0.2,))

    async def scenario():
        t = await create(m, git_repo, "crash", name="net", service_tier="default", reasoning_effort="low")
        m.scheduler.start(0.05)
        await reaches(m, t["id"], {"completed"})
        await m.scheduler.stop()
        a1, a2 = m.attempts(t["id"])
        assert (a1["attempt_number"], a1["trigger_kind"], a1["result"], a1["was_resume"]) == (1, "initial", "interrupted", 0)
        assert a1["failure_kind"] == "process_exited" and "signal 9" in a1["failure_message"]
        assert (a2["attempt_number"], a2["trigger_kind"], a2["result"], a2["was_resume"]) == (2, "auto_retry", "completed", 1)
        assert a1["codex_thread_id"] == a2["codex_thread_id"] and a1["codex_thread_id"]
        assert a2["service_tier"] == "default" and a2["reasoning_effort"] == "low"
        assert a1["finished_at"] and a2["finished_at"] and a2["git_head"]  # the state before the retry was recorded
        assert [x["attempt_number"] for x in m.attempts(t["id"])] == [1, 2]

    go(scenario())


def test_user_stop_is_never_retried(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "sleep")
        await reaches(m, t["id"], {"running"})
        await m.stop(t["id"])
        done = await reaches(m, t["id"], {"stopped"})
        await asyncio.sleep(0.6)
        assert status(m, t["id"]) == "stopped" and done["retry_count"] == 0 and len(invocations(fake_codex_state)) == 1
        assert m.attempts(t["id"])[0]["result"] == "stopped"
        await m.scheduler.stop()

    go(scenario())


def test_stop_during_the_retry_wait_cancels_the_retry(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(30.0,))

    async def scenario():
        t = await create(m, git_repo, "crash")
        await reaches(m, t["id"], {"retry_wait"})
        stopped = await m.stop(t["id"])
        assert stopped["status"] == "stopped" and stopped["next_retry_at"] is None
        m.scheduler.start(0.05)
        await asyncio.sleep(0.3)
        assert status(m, t["id"]) == "stopped" and len(invocations(fake_codex_state)) == 1
        await m.scheduler.stop()

    go(scenario())


def test_quota_is_not_retried_and_uses_no_retry(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "err 1 You've hit your usage limit. Try again later")
        done = await reaches(m, t["id"], {"waiting-for-quota", "retry_wait", "failed"})
        await asyncio.sleep(0.5)
        done = m.get(t["id"])
        assert done["status"] == "waiting-for-quota" and done["retry_count"] == 0 and "usage limit" in done["status_detail"]
        assert len(invocations(fake_codex_state)) == 1  # no immediate (or later) retry, no other model or billing path
        await m.scheduler.stop()

    go(scenario())


def test_auth_failure_is_not_retried(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "err 1 401 Unauthorized: please run codex login")
        done = await reaches(m, t["id"], {"failed", "retry_wait"})
        await asyncio.sleep(0.5)
        done = m.get(t["id"])
        assert done["status"] == "failed" and done["retry_count"] == 0 and done["last_failure_kind"] == "auth"
        assert len(invocations(fake_codex_state)) == 1
        await m.scheduler.stop()

    go(scenario())


def test_unknown_failure_is_retried_within_the_limit(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "err 1 something nobody has seen before")
        await reaches(m, t["id"], {"completed"})
        assert m.get(t["id"])["last_failure_kind"] == "unknown" and m.get(t["id"])["retry_count"] == 1
        await m.scheduler.stop()

    go(scenario())


def test_the_retry_limit_is_kept(git_repo, make_manager, monkeypatch, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.1, 0.1))
    monkeypatch.setenv("FAKE_CODEX_FORCE_MODE", "crashloop")

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "ok", max_retries=2)
        done = await reaches(m, t["id"], {"failed"})
        await asyncio.sleep(0.4)
        done = m.get(t["id"])
        assert done["status"] == "failed" and done["retry_count"] == 2
        assert "retry limit reached: 2/2" in done["status_detail"]
        assert len(invocations(fake_codex_state)) == 3  # the first run and exactly two retries
        assert [a["result"] for a in m.attempts(t["id"])] == ["interrupted", "interrupted", "failed"]
        await m.scheduler.stop()

    go(scenario())


def test_auto_retry_can_be_off(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "crash", auto_retry=False)
        done = await reaches(m, t["id"], {"failed"})
        await asyncio.sleep(0.4)
        assert status(m, t["id"]) == "failed" and len(invocations(fake_codex_state)) == 1
        with pytest.raises(TaskError):
            await create(m, git_repo, "ok", max_retries=11)
        await m.scheduler.stop()

    go(scenario())


def test_the_backoff_grows_and_retries_are_never_back_to_back(git_repo, make_manager, monkeypatch):
    import app.task_manager as tm
    waits = []
    real_timestamp = tm.timestamp
    monkeypatch.setattr(tm, "timestamp", lambda seconds_from_now=0.0: (waits.append(seconds_from_now), real_timestamp(seconds_from_now))[1])
    m = make_manager(retry_backoff_seconds=(0.2, 0.6))
    monkeypatch.setenv("FAKE_CODEX_FORCE_MODE", "crashloop")

    async def scenario():
        m.scheduler.start(0.02)
        t = await create(m, git_repo, "ok", max_retries=2)
        await reaches(m, t["id"], {"failed"})
        await m.scheduler.stop()
        assert [w for w in waits if w] == [0.2, 0.6]   # each retry is scheduled the next step of the schedule later
        a = m.attempts(t["id"])
        assert len(a) == 3
        # every retry started after the wait that was set when the previous attempt failed (next_retry_at is checked by the scheduler)
        assert all(seconds(a[i + 1]["started_at"]) >= seconds(a[i]["started_at"]) for i in range(2))

    go(scenario())


def test_no_thread_id_means_a_fresh_thread_in_the_same_worktree(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.2,))

    async def scenario():
        t = await create(m, git_repo, "crashearly")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        assert waiting["codex_thread_id"] is None  # it died before thread.started
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"completed"})
        first, second = invocations(fake_codex_state)
        assert "resume" not in second["argv"] and second["prompt"] == "crashearly"   # nothing to resume: same instruction, new thread
        assert second["cwd"] == first["cwd"] == done["worktree"]
        assert done["codex_thread_id"]  # now it has one
        assert NO_THREAD_NOTE in log_text(m, t["id"])
        assert [a["was_resume"] for a in m.attempts(t["id"])] == [0, 0]
        await m.scheduler.stop()

    go(scenario())


def test_a_turn_that_never_started_is_sent_again_not_continued(git_repo, make_manager, fake_codex_state):
    """Codex never confirmed the turn (no turn.started): the instruction may not be in the thread, so "continue" would have
    nothing to continue. Nothing can have been done yet, so the same instruction is sent again on the same thread."""
    m = make_manager(retry_backoff_seconds=(0.2,))

    async def scenario():
        t = await create(m, git_repo, "crashunstarted")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        thread = waiting["codex_thread_id"]
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"completed"})
        await m.scheduler.stop()
        first, second = invocations(fake_codex_state)
        assert second["argv"] == ["resume", thread] and second["prompt"] == "crashunstarted" != RECOVERY_PROMPT
        assert second["cwd"] == first["cwd"] and done["codex_thread_id"] == thread
        assert "had not started" in log_text(m, t["id"])

    go(scenario())


def test_the_retry_does_not_touch_the_working_tree(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(1.0,))

    async def scenario():
        t = await create(m, git_repo, "wipcrash")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        wt = Path(waiting["worktree"])
        (wt / "README.md").write_text("# demo\nedited by hand while waiting\n")  # a tracked file with uncommitted changes
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"completed"})
        await m.scheduler.stop()
        assert (wt / "wip.txt").read_text() == "work in progress\n"                 # the untracked file survived
        assert "edited by hand" in (wt / "README.md").read_text()                   # so did the modification
        assert subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head
        assert subprocess.run(["git", "-C", str(wt), "stash", "list"], capture_output=True, text=True).stdout == ""
        # the state was recorded (not changed) before the retry
        a2 = m.attempts(t["id"])[1]
        assert "wip.txt" in a2["git_status"] and a2["git_head"]
        assert "git state before the retry" in log_text(m, t["id"]) and "wip.txt" in log_text(m, t["id"])

    go(scenario())


def test_a_deleted_worktree_is_not_recreated(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(0.5,))

    async def scenario():
        t = await create(m, git_repo, "crash")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        subprocess.run(["git", "-C", str(git_repo), "worktree", "remove", "--force", waiting["worktree"]], check=True)
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"failed"})
        await m.scheduler.stop()
        assert "no longer exists" in done["status_detail"] and done["last_failure_kind"] == "worktree_missing"
        assert not Path(done["worktree"]).exists()                 # still gone: nothing was recreated
        assert len(invocations(fake_codex_state)) == 1              # and codex was not started again
        with pytest.raises(TaskError):
            await m.retry_task(t["id"])                              # a manual retry needs the worktree too

    go(scenario())


def test_retry_now_ends_the_wait(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(60.0,))

    async def scenario():
        t = await create(m, git_repo, "crash")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        assert m.present_task(waiting)["retry_in_seconds"] > 50
        for _ in range(5):  # the scheduler looking again and again does not make a retry come early
            m.tick()
        assert status(m, t["id"]) == "retry_wait" and len(invocations(fake_codex_state)) == 1
        await m.retry_task(t["id"])
        done = await reaches(m, t["id"], {"completed"})
        assert done["retry_count"] == 1 and [a["trigger_kind"] for a in m.attempts(t["id"])] == ["initial", "manual_retry"]
        assert invocations(fake_codex_state)[1]["argv"][:1] == ["resume"]

    go(scenario())


def test_disable_auto_retry_cancels_the_pending_retry(git_repo, make_manager, fake_codex_state):
    m = make_manager(retry_backoff_seconds=(60.0,))

    async def scenario():
        t = await create(m, git_repo, "crash")
        await reaches(m, t["id"], {"retry_wait"})
        done = await m.set_auto_retry(t["id"], enabled=False)
        assert done["status"] == "failed" and done["auto_retry_enabled"] == 0 and done["next_retry_at"] is None
        assert "turned off" in done["status_detail"]
        again = await m.retry_task(t["id"])  # the manual Retry is still there
        assert again["status"] == "queued" or again["status"] in ("starting", "running", "completed")
        await reaches(m, t["id"], {"completed"})
        assert len(invocations(fake_codex_state)) == 2

    go(scenario())


# ---------- manual retry ----------

def test_manual_retry_of_a_failed_task_resumes_the_same_thread(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "err 1 something odd", auto_retry=False)
        failed = await reaches(m, t["id"], {"failed"})
        done = await m.retry_task(t["id"])
        done = await reaches(m, t["id"], {"completed"})
        first, second = invocations(fake_codex_state)
        assert second["argv"] == ["resume", failed["codex_thread_id"]] and second["cwd"] == first["cwd"]
        assert done["codex_thread_id"] == failed["codex_thread_id"]
        assert m.attempts(t["id"])[1]["trigger_kind"] == "manual_retry"
        with pytest.raises(TaskError, match="only a failed or stopped"):
            await m.retry_task(t["id"])  # completed now

    go(scenario())


def test_manual_retry_of_a_stopped_task(git_repo, make_manager, fake_codex_state):
    m = make_manager()

    async def scenario():
        t = await create(m, git_repo, "sleep")
        await reaches(m, t["id"], {"running"})
        await wait_for(lambda: m.get(t["id"])["codex_thread_id"])
        await m.stop(t["id"])
        stopped = await reaches(m, t["id"], {"stopped"})
        await m.retry_task(t["id"])
        await reaches(m, t["id"], {"completed"})
        assert invocations(fake_codex_state)[1]["argv"] == ["resume", stopped["codex_thread_id"]]

    go(scenario())


def test_manual_retry_past_the_limit_needs_confirmation(git_repo, make_manager, monkeypatch):
    m = make_manager(retry_backoff_seconds=(0.1,))
    monkeypatch.setenv("FAKE_CODEX_FORCE_MODE", "crashloop")

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "ok", max_retries=1)
        await reaches(m, t["id"], {"failed"})
        await m.scheduler.stop()
        assert m.get(t["id"])["retry_count"] == 1
        with pytest.raises(TaskError) as e:
            await m.retry_task(t["id"])
        assert e.value.code == "over_limit" and status(m, t["id"]) == "failed"
        monkeypatch.delenv("FAKE_CODEX_FORCE_MODE")
        await m.retry_task(t["id"], confirm_over_limit=True)
        await reaches(m, t["id"], {"completed"})
        assert m.get(t["id"])["retry_count"] == 1  # a manual retry does not hand out new automatic retries

    go(scenario())


def test_a_new_instruction_starts_with_a_fresh_retry_budget(git_repo, make_manager):
    m = make_manager(retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "crash")
        await reaches(m, t["id"], {"completed"})
        assert m.get(t["id"])["retry_count"] == 1
        await m.send_instruction(t["id"], "ok")
        done = await reaches(m, t["id"], {"completed"})
        assert done["retry_count"] == 0 and done["last_failure_kind"] == ""
        await m.scheduler.stop()

    go(scenario())


def test_a_task_whose_worktree_could_not_be_created_fails_for_good(git_repo, make_manager, fake_codex_state, tmp_path):
    """Permanent Git state (the base branch vanished while the task waited) is not retried, and no worktree appears."""
    m = make_manager(retry_backoff_seconds=(0.1,))
    gate = tmp_path / "gate"

    async def scenario():
        subprocess.run(["git", "-C", str(git_repo), "branch", "feature"], check=True)
        a = await create(m, git_repo, f"gate {gate}", name="A")
        d = await create(m, git_repo, "ok D", base_ref="feature", depends_on=[a["id"]], name="D")
        subprocess.run(["git", "-C", str(git_repo), "branch", "-D", "feature"], check=True)  # gone before D can start
        gate.write_text("open")
        m.scheduler.start(0.05)
        done = await reaches(m, d["id"], {"failed", "retry_wait", "completed"})
        await asyncio.sleep(0.4)
        done = m.get(d["id"])
        await m.scheduler.stop()
        assert done["status"] == "failed" and done["last_failure_kind"] == "git_invalid" and done["retry_count"] == 0
        assert "base ref not found: feature" in done["status_detail"]
        assert not Path(done["worktree"]).exists()
        assert not [i for i in invocations(fake_codex_state) if i["prompt"] == "ok D"]  # Codex was never started for it

    go(scenario())


# ---------- app-server backend ----------

def test_app_server_dying_mid_turn_is_recovered_in_the_same_thread(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server", retry_backoff_seconds=(0.3,))

    async def scenario():
        t = await create(m, git_repo, "ok")
        done = await reaches(m, t["id"], {"completed"})
        thread = done["codex_thread_id"]
        await m.send_instruction(t["id"], "die")  # the shared app-server exits in the middle of the turn
        waiting = await reaches(m, t["id"], {"retry_wait"})
        assert waiting["last_failure_kind"] == "app_server_exited" and waiting["retry_count"] == 1
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"completed"})
        calls = [json.loads(line) for line in (fake_codex_state / "invocations.jsonl").read_text().splitlines()]
        assert [c["method"] for c in calls].count("thread/start") == 1                     # no second thread was ever started
        resumes = [c for c in calls if c["method"] == "thread/resume"]
        assert resumes and all(c["params"]["threadId"] == thread for c in resumes)
        turn_prompts = [c["params"]["input"][0]["text"] for c in calls if c["method"] == "turn/start"]
        assert turn_prompts == ["ok", "die", RECOVERY_PROMPT]
        assert done["codex_thread_id"] == thread and done["worktree"] == waiting["worktree"]
        assert [a["trigger_kind"] for a in m.attempts(t["id"])][-2:] == ["instruction", "auto_retry"]
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_app_server_turn_that_never_started_is_sent_again_on_the_same_thread(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server", retry_backoff_seconds=(0.2,))

    async def scenario():
        t = await create(m, git_repo, "dieearly")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        thread = waiting["codex_thread_id"]
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"completed"})
        calls = [json.loads(line) for line in (fake_codex_state / "invocations.jsonl").read_text().splitlines()]
        prompts = [c["params"]["input"][0]["text"] for c in calls if c["method"] == "turn/start"]
        assert prompts == ["dieearly", "dieearly"]                       # the instruction again, not "continue"
        assert [c["params"]["threadId"] for c in calls if c["method"] == "thread/resume"] == [thread]
        assert [c["method"] for c in calls].count("thread/start") == 1 and done["codex_thread_id"] == thread
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_app_server_thread_that_codex_never_saved_gets_a_new_thread_in_the_same_worktree(git_repo, make_manager, fake_codex_state):
    """The thread-creating turn never started and Codex has no record of the thread: nothing was ever done in it."""
    m = make_manager(backend="app-server", retry_backoff_seconds=(1.0,))

    async def scenario():
        t = await create(m, git_repo, "dieearly")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        lost = waiting["codex_thread_id"]
        (fake_codex_state / "threads" / f"{lost}.json").unlink()          # Codex never persisted it
        m.scheduler.start(0.05)
        done = await reaches(m, t["id"], {"completed"})
        await m.scheduler.stop()
        assert done["codex_thread_id"] and done["codex_thread_id"] != lost and done["worktree"] == waiting["worktree"]
        assert NO_THREAD_NOTE in log_text(m, t["id"]) and "Codex has no saved thread" in log_text(m, t["id"])
        await m.shutdown()

    go(scenario())


def test_app_server_a_started_turn_that_lost_its_thread_is_not_silently_restarted(git_repo, make_manager, fake_codex_state):
    """Only a never-started first turn may fall back to a new thread. A thread that had real work in it is not replaced."""
    m = make_manager(backend="app-server", retry_backoff_seconds=(0.2,))

    async def scenario():
        t = await create(m, git_repo, "ok")
        done = await reaches(m, t["id"], {"completed"})
        await m.send_instruction(t["id"], "die")
        waiting = await reaches(m, t["id"], {"retry_wait"})
        (fake_codex_state / "threads" / f"{waiting['codex_thread_id']}.json").unlink()
        m.scheduler.start(0.05)
        failed = await reaches(m, t["id"], {"failed"})
        await m.scheduler.stop()
        assert failed["last_failure_kind"] == "session_missing" and failed["codex_thread_id"] == waiting["codex_thread_id"]
        await m.shutdown()

    go(scenario())


def test_app_server_quota_is_not_retried(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server", retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "quota")
        await reaches(m, t["id"], {"waiting-for-quota", "retry_wait"})
        await asyncio.sleep(0.5)
        done = m.get(t["id"])
        assert done["status"] == "waiting-for-quota" and done["retry_count"] == 0
        calls = [json.loads(line) for line in (fake_codex_state / "invocations.jsonl").read_text().splitlines()]
        assert len([c for c in calls if c["method"] == "turn/start"]) == 1
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_app_server_not_subscription_is_not_retried(git_repo, make_manager, monkeypatch, fake_codex_state):
    monkeypatch.setenv("FAKE_ACCOUNT", "apiKey")
    m = make_manager(backend="app-server", retry_backoff_seconds=(0.1,))

    async def scenario():
        m.scheduler.start(0.05)
        t = await create(m, git_repo, "ok")
        await reaches(m, t["id"], {"failed", "retry_wait"})
        await asyncio.sleep(0.4)
        done = m.get(t["id"])
        assert done["status"] == "failed" and done["last_failure_kind"] == "auth" and done["retry_count"] == 0
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


# ---------- the GUI itself dies ----------

def start_gui(home, repo, prompt):
    gui = subprocess.Popen([sys.executable, str(HERE / "fake_gui.py"), str(home), str(repo), prompt],
                           stdout=subprocess.PIPE, text=True, env=os.environ.copy())
    line = gui.stdout.readline()
    assert line.startswith("READY"), line
    _, task_id, pid = line.split()
    return gui, task_id, int(pid)


def kill_hard(gui):
    os.kill(gui.pid, signal.SIGKILL)
    gui.wait()


def test_gui_crash_with_the_codex_process_still_alive_starts_nothing_twice(git_repo, settings, make_manager, fake_codex_state):
    gui, task_id, pid = start_gui(settings.home, git_repo, "sleep")
    try:
        kill_hard(gui)                                   # the GUI is gone; the codex process (own session) is not
        assert same_process(pid, process_identity(pid))
        m = make_manager(retry_backoff_seconds=(0.2,))
        assert m.recover() == [task_id]
        row = m.get(task_id)
        assert row["status"] == "running" and "without output capture" in row["status_detail"]
        assert task_id in m._adopted
        assert "still alive" in log_text(m, task_id)

        async def scenario():
            m.scheduler.start(0.05)
            await asyncio.sleep(0.5)
            assert status(m, task_id) == "running" and len(invocations(fake_codex_state)) == 1   # no second process
            os.kill(pid, signal.SIGKILL)                  # now the codex process ends unexpectedly too
            await reaches(m, task_id, {"retry_wait", "queued", "starting", "running", "completed"})
            done = await reaches(m, task_id, {"completed"})
            second = invocations(fake_codex_state)[1]
            assert second["argv"] == ["resume", done["codex_thread_id"]] and second["prompt"] == RECOVERY_PROMPT
            assert m.attempts(task_id)[-1]["trigger_kind"] == "restart_recovery"
            await m.scheduler.stop()

        go(scenario())
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_gui_crash_with_the_codex_process_gone_is_retried_once_in_the_same_thread(git_repo, settings, make_manager, fake_codex_state):
    gui, task_id, pid = start_gui(settings.home, git_repo, "sleep")
    try:
        before = settings.home
        kill_hard(gui)
        os.kill(pid, signal.SIGKILL)
        wait_gone = time.monotonic() + 5
        while process_identity(pid) and time.monotonic() < wait_gone:
            time.sleep(0.05)
        m = make_manager(retry_backoff_seconds=(0.2,))
        thread = m.get(task_id)["codex_thread_id"]
        assert m.recover() == [task_id]
        row = m.get(task_id)
        assert row["status"] == "retry_wait" and row["retry_count"] == 1 and row["last_failure_kind"] == "process_lost"
        assert m.recover() == []                         # a second look changes nothing

        async def scenario():
            m.scheduler.start(0.05)
            done = await reaches(m, task_id, {"completed"})
            await m.scheduler.stop()
            assert [i["argv"] for i in invocations(fake_codex_state)][1] == ["resume", thread]
            assert len(invocations(fake_codex_state)) == 2 and done["codex_thread_id"] == thread

        go(scenario())
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_recycled_pid_is_not_taken_for_the_codex_process(git_repo, settings, make_manager, fake_codex_state):
    """The recorded pid is alive and its command line says "codex", but it is not the process that was recorded."""
    gui, task_id, pid = start_gui(settings.home, git_repo, "sleep")
    try:
        kill_hard(gui)
        m = make_manager(retry_backoff_seconds=(5.0,))
        m.db.update_task(task_id, proc_identity=process_identity(pid).rsplit(":", 1)[0] + ":42")  # "another process, same number"
        assert m.recover() == [task_id]
        assert task_id not in m._adopted and status(m, task_id) == "retry_wait"
        assert os.path.exists(f"/proc/{pid}")  # the unrelated process was left alone, not killed
    finally:
        os.kill(pid, signal.SIGKILL)


def test_gui_restart_during_the_retry_wait_keeps_the_timer(git_repo, make_manager, fake_codex_state):
    m1 = make_manager(retry_backoff_seconds=(0.6,))

    async def first_gui():
        t = await create(m1, git_repo, "crash")
        waiting = await reaches(m1, t["id"], {"retry_wait"})
        m1.detach()
        return t["id"], waiting["next_retry_at"]

    task_id, due = go(first_gui())
    m2 = make_manager(retry_backoff_seconds=(0.6,))
    assert m2.recover() == []                      # nothing to fix: the retry is just waiting in the database
    assert m2.get(task_id)["next_retry_at"] == due and status(m2, task_id) == "retry_wait"

    async def second_gui():
        m2.scheduler.start(0.05)
        await reaches(m2, task_id, {"completed"})
        await m2.scheduler.stop()
        assert len(invocations(fake_codex_state)) == 2

    go(second_gui())


def test_gui_restart_with_retries_used_up_ends_failed(git_repo, make_manager, db):
    m = make_manager()
    base = dict(name="n", repository="/r", worktree=str(git_repo), branch="b", base_ref="main", base_sha="a",
                prompt="p", created_at="2026-01-01T00:00:00Z")
    db.create_task(id="x1", status="running", pid=2 ** 22 + 99, retry_count=3, max_retries=3, **base)
    db.create_task(id="x2", status="starting", pid=None, retry_count=0, max_retries=3, **base)
    db.create_task(id="x3", status="running", pid=2 ** 22 + 98, auto_retry_enabled=0, **base)
    assert sorted(m.recover()) == ["x1", "x2", "x3"]
    assert [db.get_task(i)["status"] for i in ("x1", "x2", "x3")] == ["failed", "retry_wait", "failed"]
    assert "retry limit reached: 3/3" in db.get_task("x1")["status_detail"]
    assert db.get_task("x2")["retry_count"] == 1
