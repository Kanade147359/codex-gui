"""Scheduled instructions: a follow-up turn for an existing Codex thread, sent once other tasks have completed AND the
thread is idle. Sync tests drive the state machine with database rows and `tick()` (no event loop, so nothing is really
started); the async ones run real turns against tests/fake_app_server.py and check the thread id on the wire."""
import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.database import Database, ScheduledError
from app.main import create_app
from app.recovery import RECOVERY_PROMPT
from app.task_manager import TaskError, TaskManager, _Turn

from conftest import FakeAppServer, FakeRunner, wait_for

THREAD = "thr-target"


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_orphan_sweep(monkeypatch):
    """Rows made with status="running" have no process behind them; the orphan sweep of tick() would (rightly) recover them
    into retry_wait. Real turns here are in `_jobs`, which the sweep skips anyway; restart recovery is a different path."""
    monkeypatch.setattr(TaskManager, "_tick_orphans", lambda self: None)


def row(db, task_id, status="completed", thread=None, **over):
    fields = dict(id=task_id, name=task_id.upper(), repository="/r", worktree="/w", branch="b", base_ref="main",
                  base_sha="abc", prompt="p", status=status, created_at="2026-01-01T00:00:00Z")
    if thread:
        fields["codex_thread_id"] = thread
    fields.update(over)
    return db.create_task(**fields)


def sched(db, task_id="x", deps=(), prompt="integrate", tier="default"):
    return db.create_scheduled(task_id, prompt, tier, list(deps))["id"]


def st(db, sid):
    return db.get_scheduled(sid)["status"]


def settle(db, task_id, status):
    """Move a task to a status through the real transition table (queued -> starting -> running -> final)."""
    path = {"completed": ["queued", "starting", "running", "completed"], "running": ["queued", "starting", "running"]}[status]
    current = db.get_task(task_id)["status"]
    for step in path[path.index(current) + 1:] if current in path else path:
        db.set_status(task_id, step)


def invocations(state, method=None):
    rows = [json.loads(line) for line in (state / "invocations.jsonl").read_text().splitlines()]
    return [r for r in rows if method is None or r["method"] == method]


# ---------- the state machine ----------

def test_sent_after_the_dependency_completes_on_the_same_thread(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    row(db, "a", status="running")
    sid = sched(db, deps=["a"])
    m.tick()
    assert st(db, sid) == "waiting_dependencies" and db.get_task("x")["status"] == "completed"
    db.set_status("a", "completed")
    m.tick()
    assert st(db, sid) == "running"
    x = db.get_task("x")
    assert x["status"] == "queued"                      # the target's own queue takes it from here
    turn = _Turn.from_json(x["pending_turn"])
    assert (turn.prompt, turn.resume_thread, turn.trigger) == ("integrate", THREAD, "scheduled_instruction")
    assert x["codex_thread_id"] == THREAD               # never a new session


def test_not_sent_until_every_dependency_completed(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="running"), row(db, "b", status="running")
    sid = sched(db, deps=["a", "b"])
    db.set_status("a", "completed")
    m.tick()
    assert st(db, sid) == "waiting_dependencies" and db.get_task("x")["status"] == "completed"
    db.set_status("b", "completed")
    m.tick()
    assert st(db, sid) == "running"


def test_a_dependency_in_retry_wait_is_not_a_failure(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="running")
    sid = sched(db, deps=["a"])
    db.set_status("a", "retry_wait", next_retry_at="2999-01-01T00:00:00.000Z")
    for _ in range(3):
        m.tick()
    assert st(db, sid) == "waiting_dependencies"        # waits for the retry's outcome
    db.set_status("a", "queued"), db.set_status("a", "starting"), db.set_status("a", "running"), db.set_status("a", "completed")
    m.tick()
    assert st(db, sid) == "running"


@pytest.mark.parametrize("final", ["failed", "stopped"])
def test_a_dependency_that_failed_for_good_blocks_the_instruction(db, make_manager, final):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="running")
    sid = sched(db, deps=["a"])
    db.set_status("a", final)
    m.tick()
    r = db.get_scheduled(sid)
    assert r["status"] == "blocked" and "A" in r["blocked_reason"]
    db.set_status("a", "queued")                        # even if the dependency is run again, a blocked one stays blocked
    m.tick()
    assert st(db, sid) == "blocked" and db.get_task("x")["status"] == "completed"


def test_a_blocked_dependency_blocks_too(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="blocked")
    sid = sched(db, deps=["a"])
    m.tick()
    assert st(db, sid) == "blocked"


def test_waits_for_the_thread_when_the_dependencies_are_done_but_the_target_is_running(db, make_manager):
    m = make_manager()
    row(db, "x", status="running", thread=THREAD), row(db, "a")
    sid = sched(db, deps=["a"])
    m.tick()
    r = db.get_scheduled(sid)
    assert r["status"] == "waiting_thread"
    assert m._scheduled_view(r)["wait_note"] == "the thread's task is running"
    assert db.get_task("x")["status"] == "running"      # nothing was sent into the running turn
    db.set_status("x", "completed")                     # the thread becomes idle
    m.tick()
    assert st(db, sid) == "running" and db.get_task("x")["status"] == "queued"


@pytest.mark.parametrize("target", ["retry_wait", "waiting_dependencies", "queued", "failed", "stopped", "interrupted",
                                    "waiting-for-quota", "blocked"])
def test_only_a_completed_task_counts_as_an_idle_thread(db, make_manager, target):
    m = make_manager()
    row(db, "x", status=target, thread=THREAD, next_retry_at="2999-01-01T00:00:00.000Z")  # (a retry that is not due yet)
    if target == "waiting_dependencies":                # a task that really waits for another one
        row(db, "p", status="running")
        db.add_dependencies("x", ["p"])
    sid = sched(db)                                     # no dependencies: "send when the thread is idle"
    for _ in range(3):
        m.tick()
    assert st(db, sid) == "waiting_thread" and db.get_task("x")["status"] == target


def test_no_dependencies_means_send_when_idle(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    m.tick()
    assert st(db, sid) == "running"


def test_a_completed_dependency_that_runs_again_sends_nothing(db, make_manager):
    """ready is not a promise: the conditions are checked again inside the claim."""
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a")
    sid = sched(db, deps=["a"])
    m.db.advance_scheduled(sid)
    assert st(db, sid) == "ready"
    db.set_status("a", "queued")                        # the user gave A another instruction
    assert m._claim_scheduled(db.get_scheduled(sid)) is False
    assert st(db, sid) == "ready" and db.get_task("x")["status"] == "completed"
    m.tick()
    assert st(db, sid) == "waiting_dependencies"


def test_the_claim_is_undone_as_a_whole_when_the_task_changed(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    db.advance_scheduled(sid)
    db.set_status("x", "queued")                        # a manual instruction won the race for the idle thread
    assert db.claim_scheduled(sid, m._queue_fields(_Turn("p", resume_thread=THREAD)), THREAD) is False
    assert st(db, sid) == "ready"                       # not "running" with nothing behind it


def test_fifo_on_one_thread_never_two_at_once(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    one, two, three = (sched(db, prompt=p) for p in ("first", "second", "third"))
    sent = []
    for _ in range(3):
        m.tick()
        assert sum(st(db, s) == "running" for s in (one, two, three)) <= 1
        x = db.get_task("x")
        if x["status"] == "queued":
            sent.append(_Turn.from_json(x["pending_turn"]).prompt)
            settle(db, "x", "completed")                # that turn finishes; the next tick finishes the instruction
    m.tick()
    assert sent == ["first", "second", "third"]
    assert [st(db, s) for s in (one, two, three)] == ["completed"] * 3 and db.get_task("x")["status"] == "completed"


def test_both_ready_only_the_older_one_goes_and_the_other_waits_for_the_thread(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    one, two = sched(db), sched(db)
    db.advance_scheduled(one), db.advance_scheduled(two)
    assert (st(db, one), st(db, two)) == ("ready", "ready")
    assert m._claim_scheduled(db.get_scheduled(two)) is False   # the younger one cannot jump the queue
    m.tick()
    assert (st(db, one), st(db, two)) == ("running", "waiting_thread")


def test_a_younger_instruction_goes_ahead_while_an_older_one_still_waits_for_its_dependency(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="running"), row(db, "b")
    one, two = sched(db, deps=["a"], prompt="after A"), sched(db, deps=["b"], prompt="after B")
    m.tick()
    assert (st(db, one), st(db, two)) == ("waiting_dependencies", "running")


def test_concurrent_evaluators_send_an_instruction_exactly_once(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="running"), row(db, "b", status="running")
    sid = sched(db, deps=["a", "b"])
    queued = []
    db.add_status_listener(lambda tid, old, new: queued.append(tid) if tid == "x" and new == "queued" else None)
    db.set_status("a", "completed"), db.set_status("b", "completed")  # both finish in the same instant
    barrier = threading.Barrier(8)

    def evaluator():
        barrier.wait()
        for _ in range(5):
            m.tick()
            db.advance_scheduled(sid)
            for r in db.ready_scheduled():
                m._claim_scheduled(r)

    threads = [threading.Thread(target=evaluator) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert queued == ["x"] and st(db, sid) == "running"


def test_the_claim_function_itself_has_one_winner(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    db.advance_scheduled(sid)
    fields = m._queue_fields(_Turn("integrate", resume_thread=THREAD, trigger="scheduled_instruction"))
    results, barrier = [], threading.Barrier(10)

    def claim():
        barrier.wait()
        results.append(db.claim_scheduled(sid, fields, THREAD))

    threads = [threading.Thread(target=claim) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_a_self_dependency_is_refused(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    with pytest.raises(ScheduledError) as e:
        db.create_scheduled("x", "p", "default", ["x"])
    assert e.value.code == "self"
    with pytest.raises(TaskError) as t:
        go(m.schedule_instruction("x", "p", ["x"]))
    assert t.value.code == "self" and db.list_scheduled() == []


def test_bad_input_is_refused_and_nothing_is_written(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a")
    for args, code in [(("x", "  ", ["a"]), "empty"), (("x", "p", ["nope"]), "missing"), (("x", "p", ["a", "a"]), "duplicate"),
                       (("ghost", "p", []), None)]:
        with pytest.raises(TaskError) as e:
            go(m.schedule_instruction(*args))
        assert code is None or e.value.code == code
    with pytest.raises(TaskError):
        go(m.schedule_instruction("x", "p", [], "not a tier!"))
    assert db.list_scheduled() == []
    with pytest.raises(ScheduledError):                 # the table is also guarded below the manager
        db.create_scheduled("x", "p", "default", ["a", "a"])


def test_speed_is_stored_per_instruction_and_defaults_to_standard(db, make_manager):
    m = make_manager()
    row(db, "x", status="running", thread=THREAD, service_tier="priority")  # a Fast task: its tier must not leak into the reservation
    tiers = []
    for given in (None, "standard", "fast", "default", "priority"):
        v = go(m.schedule_instruction("x", f"p {given}", [], given))
        tiers.append((v["service_tier"], v["speed"]))
        m.cancel_scheduled("x", v["id"])
    assert tiers == [("default", "Standard"), ("default", "Standard"), ("priority", "Fast"), ("default", "Standard"),
                     ("priority", "Fast")]


def test_cancel_before_it_runs_and_never_sent_afterwards(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="running")
    sid = sched(db, deps=["a"])
    view = m.cancel_scheduled("x", sid)
    assert view["status"] == "cancelled"
    db.set_status("a", "completed")
    for _ in range(3):
        m.tick()
        db.advance_scheduled(sid)
    assert st(db, sid) == "cancelled" and db.get_task("x")["status"] == "completed"
    assert db.claim_scheduled(sid, m._queue_fields(_Turn("p", resume_thread=THREAD)), THREAD) is False


def test_cancel_while_ready_wins_or_loses_cleanly(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    db.advance_scheduled(sid)
    assert db.cancel_scheduled(sid) is True
    assert m._claim_scheduled(db.get_scheduled(sid)) is False and db.get_task("x")["status"] == "completed"


def test_a_running_instruction_cannot_be_cancelled(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    m.tick()
    with pytest.raises(TaskError) as e:
        m.cancel_scheduled("x", sid)
    assert e.value.status == 409 and "stop the task" in str(e.value)
    with pytest.raises(TaskError) as e:                 # another task's id does not reach it either
        row(db, "y", thread="thr-y")
        m.cancel_scheduled("y", sid)
    assert e.value.status == 404


def test_cancelling_the_head_lets_the_next_one_go(db, make_manager):
    m = make_manager()
    row(db, "x", status="running", thread=THREAD)
    one, two = sched(db), sched(db)
    m.tick()
    m.cancel_scheduled("x", one)
    db.set_status("x", "completed")
    m.tick()
    assert (st(db, one), st(db, two)) == ("cancelled", "running")


def test_a_deleted_worktree_blocks_the_instruction(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    db.update_task("x", worktree_removed=1)
    m.tick()
    assert st(db, sid) == "blocked" and "worktree" in db.get_scheduled(sid)["blocked_reason"]


def test_the_instruction_follows_its_turn_to_the_end(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    ok, bad = sched(db, prompt="ok"), sched(db, prompt="bad")
    m.tick()
    settle(db, "x", "completed")
    m.tick()
    assert st(db, ok) == "completed" and st(db, bad) == "running"
    db.set_status("x", "stopped")                       # the user's Stop on the turn the instruction started
    m.tick()
    r = db.get_scheduled(bad)
    assert r["status"] == "failed" and "stopped" in r["blocked_reason"] and r["finished_at"]


def test_a_paused_or_recovering_turn_keeps_the_instruction_running(db, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    m.tick()
    for status in ("starting", "running", "retry_wait"):
        db.set_status("x", status, **({"next_retry_at": "2999-01-01T00:00:00.000Z"} if status == "retry_wait" else {}))
        m.tick()
        assert st(db, sid) == "running"                 # not re-sent, not failed: the task's own recovery owns it
    db.set_status("x", "queued"), db.set_status("x", "starting"), db.set_status("x", "running"), db.set_status("x", "interrupted")
    m.tick()
    assert st(db, sid) == "running"                     # interrupted: the user resumes it, the same turn goes on


def test_scheduled_counts_for_the_dashboard(db, make_manager):
    m = make_manager()
    row(db, "x", status="running", thread=THREAD), row(db, "y", thread="thr-y"), row(db, "a", status="running")
    sched(db, "x"), sched(db, "x", deps=["a"]), sched(db, "y", deps=["a"])
    m.tick()
    view = {t["id"]: t for t in m.list_tasks_view()}
    assert (view["x"]["scheduled_pending"], view["x"]["scheduled_ready"]) == (2, 0)
    db.set_status("x", "completed")
    m.tick()                                            # the first one is sent; the other still waits for A
    assert {t["id"]: (t["scheduled_pending"], t["scheduled_ready"]) for t in m.list_tasks_view()}["x"] == (1, 0)
    detail = m.present_task(m.get("x"))
    assert [s["status"] for s in detail["scheduled_instructions"]] == ["running", "waiting_dependencies"]
    assert detail["scheduled_instructions"][1]["dependencies"] == [{"id": "a", "name": "A", "status": "running"}]


# ---------- restart ----------

def restarted(settings, old):
    """What a GUI restart leaves: the same database file, a new process-level manager."""
    old.detach()
    db2 = Database(settings.db_path)
    return db2, TaskManager(settings, db2, FakeRunner(), None)


def test_instructions_survive_a_restart_and_are_evaluated_again(db, settings, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD), row(db, "a", status="running"), row(db, "busy", status="running", thread="thr-b")
    waiting, thread_wait = sched(db, deps=["a"]), sched(db, "busy")
    m.tick()
    assert (st(db, waiting), st(db, thread_wait)) == ("waiting_dependencies", "waiting_thread")
    db2, m2 = restarted(settings, m)
    try:
        assert [(r["id"], r["status"]) for r in db2.list_scheduled()] == [(waiting, "waiting_dependencies"), (thread_wait, "waiting_thread")]
        db2.set_status("a", "completed"), db2.set_status("busy", "completed")
        m2.recover(), m2.tick()
        assert st(db2, waiting) == "running" and st(db2, thread_wait) == "running"
        assert db2.get_task("x")["status"] == "queued" and db2.get_task("busy")["status"] == "queued"
    finally:
        db2.close()


def test_a_running_instruction_is_not_sent_again_after_a_restart(db, settings, make_manager):
    m = make_manager()
    row(db, "x", thread=THREAD)
    sid = sched(db)
    m.tick()
    before = db.get_task("x")["pending_turn"]
    db2, m2 = restarted(settings, m)
    claims = []
    db2.add_status_listener(lambda tid, old, new: claims.append(new))
    try:
        for _ in range(3):
            m2.recover(), m2.tick()
        assert st(db2, sid) == "running" and db2.get_task("x")["pending_turn"] == before   # one pending turn, nothing re-sent
        assert claims == []                             # no new status change was made by the scheduler
    finally:
        db2.close()


# ---------- real turns against the fake app-server ----------

async def create(m, repo, prompt="ok", **kw):
    return await m.create_task(repository=str(repo), prompt=prompt, name=kw.pop("name", "task"), **kw)


async def status_of(m, task_id, wanted, timeout=15.0):
    return await wait_for(lambda: m.get(task_id)["status"] in wanted and m.get(task_id), timeout)


async def instruction_status(m, sid, wanted, timeout=15.0):
    return await wait_for(lambda: m.db.get_scheduled(sid)["status"] in wanted and m.db.get_scheduled(sid), timeout)


def turn_starts(state):
    return [c["params"] for c in invocations(state, "turn/start")]


def test_turn_two_is_sent_to_the_same_thread_after_the_dependency_completes(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        m.scheduler.start(0.05)
        x = await create(m, git_repo, "ok first", name="X")
        done = await status_of(m, x["id"], {"completed"})
        thread = done["codex_thread_id"]
        a = await create(m, git_repo, "sleep", name="A")                # runs until it is steered
        await wait_for(lambda: m.get(a["id"])["status"] == "running")
        view = await m.schedule_instruction(x["id"], "ok integrate A", [a["id"]], "standard")
        assert view["status"] == "waiting_dependencies" and view["dependencies"][0]["id"] == a["id"]
        await asyncio.sleep(0.4)
        assert [p["input"][0]["text"] for p in turn_starts(fake_codex_state)] == ["ok first", "sleep"]  # nothing sent yet
        await m.send_instruction(a["id"], "stop sleeping")             # steered: A completes
        await status_of(m, a["id"], {"completed"})
        await instruction_status(m, view["id"], {"completed"})
        end = await status_of(m, x["id"], {"completed"})
        assert end["codex_thread_id"] == thread and end["worktree"] == done["worktree"] and end["branch"] == done["branch"]
        starts = invocations(fake_codex_state, "thread/start")
        assert len(starts) == 2                                         # X's thread and A's: no third session
        resumes = [c["params"]["threadId"] for c in invocations(fake_codex_state, "thread/resume")]
        assert resumes == [thread]
        sent = [p for p in turn_starts(fake_codex_state) if p["input"][0]["text"] == "ok integrate A"]
        assert len(sent) == 1 and sent[0]["threadId"] == thread
        turns = m.db.list_turns(x["id"])
        assert [(t["turn"], t["thread_id"]) for t in turns] == [(1, thread), (2, thread)]
        assert [a_["trigger_kind"] for a_ in m.db.list_attempts(x["id"])] == ["initial", "scheduled_instruction"]
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_the_speed_of_the_reservation_beats_the_task_and_the_last_turn(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        m.scheduler.start(0.05)
        x = await create(m, git_repo, "ok first", name="X", service_tier="priority")   # the task itself is Fast
        await status_of(m, x["id"], {"completed"})
        first = await m.send_instruction(x["id"], "ok manual fast", service_tier="fast")  # and the last turn was Fast
        await status_of(m, x["id"], {"completed"})
        std = await m.schedule_instruction(x["id"], "ok scheduled standard", [], "standard")
        await instruction_status(m, std["id"], {"completed"})
        fast = await m.schedule_instruction(x["id"], "ok scheduled fast", [], "fast")
        await instruction_status(m, fast["id"], {"completed"})
        tier = {p["input"][0]["text"]: p.get("serviceTierForTurn") for p in turn_starts(fake_codex_state)}
        assert tier["ok manual fast"] == "priority"
        assert tier["ok scheduled standard"] == "default"               # explicit Standard, not inherited from the Fast turn
        assert tier["ok scheduled fast"] == "priority"
        rows = {t["turn"]: t["service_tier"] for t in m.db.list_turns(x["id"])}
        assert [rows[i] for i in (3, 4)] == ["default", "priority"]     # what was actually used is recorded per turn
        attempts = m.db.list_attempts(x["id"])
        assert [(a["trigger_kind"], a["service_tier"]) for a in attempts][-2:] == [("scheduled_instruction", "default"),
                                                                                  ("scheduled_instruction", "priority")]
        assert m.get(x["id"])["service_tier"] == "priority"             # the task's own tier did not change
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_waits_for_the_running_turn_then_sends_when_the_thread_is_idle(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        m.scheduler.start(0.05)
        a = await create(m, git_repo, "ok A", name="A")
        await status_of(m, a["id"], {"completed"})
        x = await create(m, git_repo, "ok first", name="X")
        await status_of(m, x["id"], {"completed"})
        await m.send_instruction(x["id"], "sleep")                      # X's thread is running a long turn
        await wait_for(lambda: m.get(x["id"])["status"] == "running")
        view = await m.schedule_instruction(x["id"], "ok after the sleep", [a["id"]], "standard")
        got = await instruction_status(m, view["id"], {"waiting_thread"})  # A is done, but the thread is not idle
        assert "running" in m._scheduled_view(got)["wait_note"]
        await asyncio.sleep(0.4)
        assert "ok after the sleep" not in [p["input"][0]["text"] for p in turn_starts(fake_codex_state)]
        await m.send_instruction(x["id"], "end")                        # steer: the running turn completes
        await instruction_status(m, view["id"], {"completed"})
        texts = [p["input"][0]["text"] for p in turn_starts(fake_codex_state) if p["threadId"] == m.get(x["id"])["codex_thread_id"]]
        assert texts == ["ok first", "sleep", "ok after the sleep"]
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_two_instructions_run_one_after_the_other_on_the_thread(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        m.scheduler.start(0.05)
        x = await create(m, git_repo, "ok first", name="X")
        await status_of(m, x["id"], {"completed"})
        one = await m.schedule_instruction(x["id"], "sleep one", [], "standard")
        two = await m.schedule_instruction(x["id"], "ok two", [], "standard")
        await wait_for(lambda: m.get(x["id"])["status"] == "running")
        await asyncio.sleep(0.5)
        assert m.db.get_scheduled(one["id"])["status"] == "running" and m.db.get_scheduled(two["id"])["status"] == "waiting_thread"
        assert [p["input"][0]["text"] for p in turn_starts(fake_codex_state)] == ["ok first", "sleep one"]
        await m.send_instruction(x["id"], "end")
        await instruction_status(m, two["id"], {"completed"})
        assert m.db.get_scheduled(one["id"])["status"] == "completed"
        assert [p["input"][0]["text"] for p in turn_starts(fake_codex_state)] == ["ok first", "sleep one", "ok two"]
        one_row, two_row = m.db.get_scheduled(one["id"]), m.db.get_scheduled(two["id"])
        assert one_row["finished_at"] <= two_row["started_at"]
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_an_unexpected_stop_is_recovered_by_the_task_and_the_instruction_is_not_sent_again(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server", retry_backoff_seconds=(0.2,))

    async def scenario():
        m.scheduler.start(0.05)
        x = await create(m, git_repo, "ok first", name="X")
        done = await status_of(m, x["id"], {"completed"})
        thread = done["codex_thread_id"]
        view = await m.schedule_instruction(x["id"], "die in the middle", [], "standard")   # the app-server exits mid-turn
        await instruction_status(m, view["id"], {"running"})
        await status_of(m, x["id"], {"retry_wait", "completed"})
        end = await status_of(m, x["id"], {"completed"})
        await instruction_status(m, view["id"], {"completed"})
        for _ in range(5):
            m.tick()
        await asyncio.sleep(0.3)
        texts = [p["input"][0]["text"] for p in turn_starts(fake_codex_state)]
        assert texts == ["ok first", "die in the middle", RECOVERY_PROMPT]    # recovery, not the instruction a second time
        assert [c["params"]["threadId"] for c in invocations(fake_codex_state, "thread/resume")][-1] == thread
        assert len(invocations(fake_codex_state, "thread/start")) == 1 and end["codex_thread_id"] == thread
        assert [a["trigger_kind"] for a in m.db.list_attempts(x["id"])] == ["initial", "scheduled_instruction", "auto_retry"]
        assert m.db.get_scheduled(view["id"])["status"] == "completed"
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_a_turn_that_never_started_is_recovered_by_the_task_not_by_the_scheduler(git_repo, make_manager, fake_codex_state):
    """The scheduler's claim happens once. If Codex never confirmed the turn, the TASK's recovery repeats that one pending
    turn (the thread never saw it); the instruction row stays `running` and is never claimed again."""
    m = make_manager(backend="app-server", retry_backoff_seconds=(0.2,))

    async def scenario():
        m.scheduler.start(0.05)
        x = await create(m, git_repo, "ok first", name="X")
        await status_of(m, x["id"], {"completed"})
        view = await m.schedule_instruction(x["id"], "dieearly please", [], "fast")
        await instruction_status(m, view["id"], {"completed"})
        texts = [p["input"][0]["text"] for p in turn_starts(fake_codex_state)]
        assert texts == ["ok first", "dieearly please", "dieearly please"]
        assert [a["trigger_kind"] for a in m.db.list_attempts(x["id"])] == ["initial", "scheduled_instruction", "auto_retry"]
        assert [p.get("serviceTierForTurn") for p in turn_starts(fake_codex_state)][1:] == ["priority", "priority"]
        assert m.db.get_scheduled(view["id"])["started_at"]
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_stop_during_the_instruction_turn_fails_it_and_holds_the_next_one(git_repo, make_manager, fake_codex_state):
    m = make_manager(backend="app-server")

    async def scenario():
        m.scheduler.start(0.05)
        x = await create(m, git_repo, "ok first", name="X")
        await status_of(m, x["id"], {"completed"})
        one = await m.schedule_instruction(x["id"], "sleep one", [], "standard")
        two = await m.schedule_instruction(x["id"], "ok two", [], "standard")
        await wait_for(lambda: m.get(x["id"])["status"] == "running")
        await m.stop(x["id"])
        await status_of(m, x["id"], {"stopped"})
        failed = await instruction_status(m, one["id"], {"failed"})
        assert "stopped" in failed["blocked_reason"]
        await asyncio.sleep(0.4)
        assert m.db.get_scheduled(two["id"])["status"] == "waiting_thread"   # a Stop is not undone by the next reservation
        assert "ok two" not in [p["input"][0]["text"] for p in turn_starts(fake_codex_state)]
        await m.scheduler.stop()
        await m.shutdown()

    go(scenario())


def test_http_api_schedule_list_and_cancel(git_repo, settings, tmp_path):
    settings.backend = "app-server"
    app = create_app(settings, FakeRunner(), FakeAppServer("fake", settings.subscription_only))
    with TestClient(app) as c:
        def create_task(prompt, name):
            r = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": prompt, "name": name})
            assert r.status_code == 200, r.text
            return r.json()["id"]

        def wait(task_id, wanted):
            import time
            end = time.time() + 20
            while time.time() < end:
                t = c.get(f"/api/tasks/{task_id}").json()
                if t["status"] in wanted:
                    return t
                time.sleep(0.05)
            raise AssertionError(f"{task_id} never reached {wanted}")

        x = create_task("ok first", "X")
        a = create_task("sleep", "A")
        wait(x, {"completed"})
        r = c.post(f"/api/tasks/{x}/scheduled", json={"prompt": "ok after A", "depends_on": [a], "service_tier": "fast"})
        assert r.status_code == 200 and r.json()["status"] == "waiting_dependencies" and r.json()["speed"] == "Fast"
        sid = r.json()["id"]
        bad = c.post(f"/api/tasks/{x}/scheduled", json={"prompt": "p", "depends_on": [x]})
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "self"
        listing = c.get(f"/api/tasks/{x}/scheduled").json()["scheduled"]
        assert [s["id"] for s in listing] == [sid]
        detail = c.get(f"/api/tasks/{x}").json()
        assert detail["scheduled_instructions"][0]["id"] == sid and detail["scheduled_pending"] == 1
        dash = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}
        assert (dash[x]["scheduled_pending"], dash[x]["scheduled_ready"]) == (1, 0)
        cancelled = c.delete(f"/api/tasks/{x}/scheduled/{sid}")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert c.delete(f"/api/tasks/{x}/scheduled/{sid}").status_code == 409
        assert c.delete(f"/api/tasks/{x}/scheduled/9999").status_code == 404
        c.post(f"/api/tasks/{a}/messages", json={"prompt": "wake up"})
        wait(a, {"completed"})
        import time
        time.sleep(0.5)
        assert c.get(f"/api/tasks/{x}/scheduled").json()["scheduled"][0]["status"] == "cancelled"
        assert len(c.get(f"/api/tasks/{x}/usage").json()["turns"]) == 1       # the cancelled instruction was never sent
