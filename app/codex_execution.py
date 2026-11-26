"""Codex app-server execution, kept separate from provider-neutral scheduling."""
from __future__ import annotations
import asyncio
import json
import time
from typing import TYPE_CHECKING

from . import completion, ctx_guard, recovery
from .appserver import AppServerError, CLOSED
from .codex_runner import approval_params, nested, task_config
from .ctx_config import task_cwd
from .models import now_iso
from .notifications import log_entry
from .procinfo import process_identity
from .recovery import Failure, NO_THREAD_NOTE
from .usage import is_quota_error, parse_token_usage, quota_exhausted


if TYPE_CHECKING:
    from .logstore import TaskLog
    from .task_manager import _Turn


def error_kind(error):
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        return info
    return next(iter(info)) if isinstance(info, dict) and info else ""


class CodexExecution:
    # ----- app-server backend -----

    def _note_overlap(self, task_id: str) -> None:
        """Remember that tasks ran together: their quota change cannot be told apart afterwards."""
        others = [t for t in self._active_turns if t != task_id]
        if others:
            self._overlap.update(others)
            self._overlap.add(task_id)

    def _thread_params(self, task: dict) -> dict:
        """thread/start and thread/resume carry the same settings, so a resumed thread never drifts."""
        params = {"cwd": task_cwd(task), "serviceTier": task["service_tier"], "config": nested(task_config(task)),
                  **approval_params(task)}
        if task["model"]:
            params["model"] = task["model"]
        return params

    async def _run_app_server_turn(self, task_id: str, log: TaskLog, turn: _Turn) -> None:
        from .task_manager import TaskError, INSTRUCTION_LOG_CHARS
        task = self.get(task_id)
        started_at = now_iso()
        compact = turn.kind == "compact"
        try:
            client = await self.client()
            await self._require_subscription(client)
        except AppServerError as e:
            log.add_system(f"cannot use Codex: {e}")
            self._settle(task_id, log, turn, "failed", failure=recovery.classify_app_server_error(e))
            return
        except TaskError as e:
            log.add_system(f"cannot use Codex: {e}")
            self._settle(task_id, log, turn, "failed", failure=Failure("auth", recovery.NON_RETRYABLE, str(e)[:500]))
            return

        before = await self.read_limits("turn_start", task_id, force_history=True)
        if quota_exhausted(before):
            # Codex says the included usage is exhausted: do not start a turn that cannot run, and do not retry.
            log.add_system("Codex reports that ordinary usage is not available (rate limit reached); not starting the turn.")
            self._settle(task_id, log, turn, "failed", failure=Failure(
                "quota", recovery.QUOTA, f"rate limit reached ({before.get('reached_type') or 'ordinary usage unavailable'})"))
            return
        self.db.update_task(task_id, five_hour_used_before=before and before["five_hour_used"],
                            weekly_used_before=before and before["weekly_used"],
                            five_hour_used_after=None, weekly_used_after=None, quota_overlap=0)

        try:
            while True:
                try:
                    res = await client.request(
                        "thread/resume" if turn.resume_thread else "thread/start",
                        {**self._thread_params(task),
                         **({"threadId": turn.resume_thread, "excludeTurns": True} if turn.resume_thread else
                            {"serviceName": "codex-gui",
                             **({"developerInstructions": self.runner.instructions} if self.runner.instructions else {})})},
                        timeout=60)
                    break
                except AppServerError as e:
                    if turn.fresh_ok and turn.resume_thread and recovery.classify_app_server_error(e).kind == "session_missing":
                        # The turn that created this thread never started and Codex never saved the thread: nothing was
                        # ever done in it, so the same instruction may start a new thread (same worktree, same branch).
                        log.add_system(f"Codex has no saved thread {turn.resume_thread} (the process died before the first turn started). "
                                       "Starting a new Codex thread in the same worktree with the same instruction.")
                        log.add_system(NO_THREAD_NOTE)
                        turn.resume_thread = turn.thread_id = None
                        turn.fresh_ok = False
                        continue
                    raise
            thread = res["thread"]
            thread_id = thread["id"]
        except AppServerError as e:
            log.add_system(f"codex could not open the thread: {e}")
            failure = recovery.classify_app_server_error(e)
            self._settle(task_id, log, turn, "failed", failure=Failure(failure.kind, failure.category, f"thread: {failure.message}"[:500]))
            return
        except (KeyError, TypeError) as e:
            log.add_system(f"codex could not open the thread: {e}")
            self._settle(task_id, log, turn, "failed", failure=Failure("unknown", recovery.UNKNOWN, f"thread: {e}"[:500]))
            return
        turn.thread_id = thread_id
        model = res.get("model") or task["model"] or None
        if turn.resume_thread:
            log.add_system(f"turn: resuming Codex thread {thread_id}")
            if thread_id != turn.resume_thread:
                log.add_system(f"WARNING: asked to resume Codex thread {turn.resume_thread} but codex reported "
                               f"{thread_id}; the thread was NOT continued, so cached input is not reused")
                self._settle(task_id, log, turn, "failed", failure=Failure("session_mismatch", recovery.NON_RETRYABLE,
                              "Codex did not resume the requested session; no turn was sent"))
                return
        else:
            self._persist_thread(task_id, turn, thread_id)
            log.add_system(f"turn: started Codex thread {thread_id}")
            log.add_system(f"codex session id: {thread_id}")
        if compact:
            log.add_system("compacting the thread (thread/compact/start)")
        else:
            log.add_system("instruction:\n" + turn.prompt[:INSTRUCTION_LOG_CHARS] +
                           (f"\n… (+{len(turn.prompt) - INSTRUCTION_LOG_CHARS} chars)" if len(turn.prompt) > INSTRUCTION_LOG_CHARS else ""))
        log.add_system(f"model {model or 'default'}, effort {task['reasoning_effort']}, tier {task['service_tier']}, "
                       f"sandbox {task['sandbox']}, auto approval {'on' if task['auto_approval'] else 'off'}")

        queue = client.subscribe(thread_id)
        observer = await self.ctx.observer(task, model)
        turn_tier = turn.service_tier or task["service_tier"]
        info = self._active_turns[task_id] = {"thread": thread_id, "turn": None}
        self._note_overlap(task_id)
        try:
            if compact:
                await client.request("thread/compact/start", {"threadId": thread_id}, timeout=60)
            else:
                rules = json.loads(task["completion_contract"] or "{}")
                prompt = turn.prompt + ("\nCompletion contract: " + json.dumps(rules) if rules else "")
                params = {"threadId": thread_id, "input": self._image_input(prompt, turn.attachment_ids),
                          "outputSchema": completion.SCHEMA}
                if task["reasoning_effort"] not in ("", "default"):
                    params["effort"] = task["reasoning_effort"]
                # The speed is chosen for THIS turn only (Send Standard / Send Fast): serviceTierForTurn does not change the
                # thread's own default, whereas serviceTier would (measured with codex-cli 0.159.2).
                params["serviceTierForTurn"] = turn_tier
                started = await client.request("turn/start", params, timeout=60)
                info["turn"] = started["turn"]["id"]
        except (AppServerError, KeyError, TypeError) as e:
            client.unsubscribe(thread_id)
            log.add_system(f"codex could not start the turn: {e}")
            if isinstance(e, AppServerError):
                failure = recovery.classify_app_server_error(e)
                failure = Failure(failure.kind, failure.category, f"turn/start: {failure.message}"[:500])
            else:
                failure = Failure("unknown", recovery.UNKNOWN, f"turn/start: {e}"[:500])
            self._settle(task_id, log, turn, "failed", failure=failure)
            return
        self.db.set_status(task_id, "running", pid=client.pid, proc_identity=process_identity(client.pid), started_at=started_at)
        if task_id in self._stop_requested:
            await self._interrupt(task_id)

        final, error, usage, lost = None, None, None, None
        try:
            while True:
                try:
                    method, params = await asyncio.wait_for(queue.get(), 1.0)
                except asyncio.TimeoutError:
                    if time.monotonic() > self._stop_deadline.get(task_id, float("inf")):
                        log.add_system("codex did not finish after the interrupt; giving up on the turn")
                        break
                    continue
                if method == CLOSED["method"]:
                    raise AppServerError(params.get("reason", "app-server closed"), kind="closed")
                tid = params.get("turnId") or (params.get("turn") or {}).get("id")
                if info["turn"] and tid and tid != info["turn"]:
                    continue  # an event of some other turn of this thread
                if method == "turn/started" and not info["turn"] and tid:
                    info["turn"] = tid
                    if task_id in self._stop_requested:
                        await self._interrupt(task_id)
                if method in ("turn/started", "turn/completed") or method.startswith("item/"):
                    self._mark_started(task_id, turn)
                guard = observer.feed(method, params)
                if guard and task_id not in self._guard_stops:
                    # The retry guard owns this stop (an error Codex would keep retrying, or the same tool failing again
                    # and again): interrupt the turn instead of letting it loop, and never retry it.
                    self._guard_stops[task_id] = {"reason": guard["interrupt"], "message": guard["message"]}
                    log.add_system("retry guard: " + guard["message"])
                    await self._interrupt(task_id)
                if method == "thread/tokenUsage/updated":
                    usage = parse_token_usage(params.get("tokenUsage")) or usage
                    if usage and not compact:
                        self.db.update_task(task_id, context_tokens=usage["context_tokens"],
                                            context_window=usage["context_window"])
                elif method == "error" and not params.get("willRetry") and isinstance(params.get("error"), dict):
                    error = params["error"]
                entry = log_entry(method, params)
                if entry:
                    log.add_event(entry[0], entry[1], params)
                if method == "item/completed" and params.get("turnId") == info.get("turn"):
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                        turn.final_text = item.get("text") if isinstance(item.get("text"), str) else ""
                if method == "turn/completed":
                    final = params.get("turn") or {}
                    for item in final.get("items") or []:
                        if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                            turn.final_text = item.get("text") if isinstance(item.get("text"), str) else ""
                    if final.get("status") == "completed" and not compact:
                        self._record_completion_pending(task_id, turn.final_text)
                    break
        except AppServerError as e:
            log.add_system(f"codex app-server failed during the turn: {e}")
            lost = e
        finally:
            client.unsubscribe(thread_id)

        await self._finish_app_server_turn(task_id, log, turn, final, error, lost, usage, started_at, model,
                                           observer=observer, turn_tier=turn_tier)

    async def _interrupt(self, task_id: str) -> None:
        info = self._active_turns.get(task_id)
        if not info or not info.get("turn"):
            return  # the turn id is not known yet; the loop interrupts as soon as it is
        self._stop_deadline.setdefault(task_id, time.monotonic() + self.settings.stop_grace_seconds)
        try:
            await (await self.client()).request("turn/interrupt", {"threadId": info["thread"], "turnId": info["turn"]}, timeout=15)
        except AppServerError:
            pass  # the deadline in the event loop ends the turn anyway

    async def _finish_app_server_turn(self, task_id, log, turn, final, error, lost, usage, started_at, model,
                                      observer=None, turn_tier=None) -> None:
        task = self.get(task_id)
        guard_stop = self._guard_stops.get(task_id)
        compact = turn.kind == "compact"
        turn_error = (final or {}).get("error") or error
        kind = error_kind(turn_error)
        if usage:
            self._store_turn(task_id, log, turn.thread_id or "", usage["total"], kind=turn.kind,
                             status=(final or {}).get("status", "interrupted"), turn_id=self._active_turns.get(task_id, {}).get("turn"),
                             started_at=started_at, model=model, effort=task["reasoning_effort"],
                             context=None if compact else usage, turn_tier=turn_tier or turn.service_tier, observer=observer)
            if not compact and usage.get("context_tokens") is not None:
                self.ctx.record_zone_change(self.get(task_id), usage["context_tokens"], model or task["model"], usage.get("context_window"))
        if compact:  # the size after compaction is only known from the next turn's first request
            self.db.update_task(task_id, context_tokens=None)

        # A lost server has no attributable end-of-turn quota snapshot. Querying it here can restart the
        # server or wait on a dying transport before recording the process failure.
        after = None if lost is not None else await self.read_limits("turn_end", task_id, force_history=True)
        fields = {"finished_at": now_iso(), "exit_code": None}
        if after:
            fields.update(five_hour_used_after=after["five_hour_used"], weekly_used_after=after["weekly_used"])
        if task_id in self._overlap:
            fields["quota_overlap"] = 1

        status_text = (final or {}).get("status")
        failure = None
        stop_reason = ""
        if task_id in self._stop_requested:
            status = "interrupted" if self._shutting_down else "stopped"
        elif guard_stop and status_text != "completed":
            # The guard stopped this turn: a failure that is NOT retried, with the reason on the task for the UI.
            status, stop_reason = "failed", guard_stop["reason"]
            failure = Failure(stop_reason, recovery.NON_RETRYABLE, guard_stop["message"])
            fields["failure_source"] = "guard"
        elif status_text == "completed":
            status = "completed"
        elif status_text == "interrupted":
            status = "stopped"
        else:
            if lost is not None and not final:  # the app-server went away or stopped answering: a process failure
                failure = recovery.classify_app_server_error(lost)
            else:
                failure = recovery.classify_turn_error(kind, str((turn_error or {}).get("message") or "the turn failed"))
            # Quota is decided from what Codex said (error kind or limits snapshot), never from a failed run alone.
            if is_quota_error(kind) or quota_exhausted(after):
                failure = Failure("quota", recovery.QUOTA,
                                  f"Codex usage limit reached ({kind or (after or {}).get('reached_type') or 'unavailable'})")
            else:
                fields["failure_source"] = "codex" if final else "gui"
            stop_reason = (observer.stop_reason if observer else "") or ctx_guard.stop_reason_for_error(kind)
            status = "failed"  # _settle turns it into waiting-for-quota, retry_wait or failed
        log.add_system(f"turn finished: {status_text or 'no turn/completed'} -> {status}")
        await self.refresh_git_summary(task_id)
        fields["stop_reason"] = stop_reason  # "" on success: a later turn clears an old context-overflow block
        if stop_reason:
            self.db.add_context_event(task_id, "retry_guard", "critical", ctx_guard.STOP_MESSAGES.get(stop_reason, stop_reason), now_iso(),
                                      None, {"reason": stop_reason})
        if status == "completed" and not compact:
            fields.update(await self._completion_fields(task_id, turn.final_text))
        if task_id in self._stop_requested:
            status = "interrupted" if self._shutting_down else "stopped"
            fields.update(task_outcome="incomplete", outcome_reason="Stopped before completion was accepted.", outcome_source="gate")
        # Last, and with nothing awaited afterwards: once the status is terminal a new turn may be started.
        self._settle(task_id, log, turn, status, failure=failure, **fields)

