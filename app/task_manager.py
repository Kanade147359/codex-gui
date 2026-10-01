"""Creates tasks (branch + worktree), runs Codex turns in parallel, and keeps the DB in sync.

1 task = 1 branch = 1 worktree = 1 Codex thread. With the app-server backend (default) one shared `codex app-server`
hosts every task's thread: the first instruction is `thread/start` + `turn/start`, every later one `thread/resume` +
`turn/start` on the same thread (the history stays inside Codex; the GUI never re-sends it). The older `exec`
backend does the same with `codex exec` / `codex exec resume`.
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import git_manager as git
from .appserver import CLOSED, AppServerClient, AppServerError
from .codex_runner import CodexRunner, approval_params, nested, pid_is_codex, task_config, terminate_process
from .config import Settings
from .database import Database
from .instructions import load_instructions
from .logstore import TaskLog, read_log
from .notifications import log_entry
from .usage import (
    REQUIRED_KEYS, cache_hit_rate, context_status, dumps, extract_usage, is_quota_error, parse_rate_limits,
    parse_token_usage, quota_exhausted, turn_delta,
)
from .models import (
    ACTIVE_STATUSES, EFFORT_RE, ESCALATION, SANDBOXES, SERVICE_TIER_RE, TERMINAL_STATUSES, VERBOSITIES,
    branch_name, make_task_id, now_iso, worktree_path,
)

READER_DRAIN_SECONDS = 5.0
INSTRUCTION_LOG_CHARS = 4000
FEATURE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
OBSERVED_NOTE = "Observed only; not an exact per-task cost."
OBSERVED_OVERLAP_NOTE = "Other tasks were running at the same time, so this cannot be attributed to this task."


@dataclass
class _Turn:
    """What one Codex turn (one process with `exec`, one turn/start with the app-server) is about."""
    prompt: str
    resume_thread: Optional[str] = None  # None: this turn starts a new Codex thread
    thread_id: Optional[str] = None      # from thread.started / thread/start
    kind: str = "turn"                   # "turn" | "compact"


class TaskError(Exception):
    """A problem the API should report to the user. `code` lets the UI react (e.g. "dirty")."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def error_kind(error: Optional[dict]) -> str:
    """Name of a TurnError's codexErrorInfo ("usageLimitExceeded", "httpConnectionFailed", ...), or ""."""
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if isinstance(info, str):
        return info
    return next(iter(info)) if isinstance(info, dict) and info else ""


class TaskManager:
    def __init__(self, settings: Settings, db: Database, runner: Optional[CodexRunner] = None,
                 app_server: Optional[AppServerClient] = None):
        self.settings = settings
        self.db = db
        self.runner = runner or CodexRunner(settings.codex_bin, settings.subscription_only)
        self.runner.instructions = load_instructions(settings.instructions_path)
        self._app_server = app_server
        self._jobs: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._active_turns: dict[str, dict] = {}   # task id -> {"thread", "turn"} of the turn being run (app-server)
        self._stop_requested: set[str] = set()
        self._stop_deadline: dict[str, float] = {}
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False
        self._slots = asyncio.Semaphore(settings.max_concurrent) if settings.max_concurrent > 0 else None
        self._limits: Optional[dict] = None
        self._limits_at = 0.0
        self._limits_history_at = 0.0
        self._overlap: set[str] = set()

    @property
    def uses_app_server(self) -> bool:
        return self.settings.backend == "app-server"

    # ---------- lookup ----------

    def log_path(self, task_id: str) -> Path:
        return self.settings.logs_dir / f"{task_id}.jsonl"

    def get(self, task_id: str) -> dict:
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskError("task not found", 404)
        return task

    def read_log(self, task_id: str, offset: int = 0) -> tuple[list[dict], int]:
        self.get(task_id)
        return read_log(self.log_path(task_id), offset)

    # ---------- the shared app-server ----------

    async def client(self) -> AppServerClient:
        """The running app-server client (started on first use). Raises AppServerError if it cannot be started."""
        if self._app_server is None:
            self._app_server = AppServerClient(self.settings.codex_bin, self.settings.subscription_only)
            self._app_server.on_global(self._on_global_notification)
        await self._app_server.start()
        return self._app_server

    async def _require_subscription(self, client: AppServerClient) -> None:
        """Subscription-only: the stored Codex login must be a ChatGPT account. No API-key fallback, ever."""
        if not self.settings.subscription_only:
            return
        info = await client.request("account/read", {"refreshToken": False}, timeout=20)
        account = info.get("account") if isinstance(info, dict) else None
        kind = account.get("type") if isinstance(account, dict) else None
        if kind != "chatgpt":
            raise TaskError(
                "Codex is not signed in with a ChatGPT (subscription) account "
                f"({'API key' if kind == 'apiKey' else 'not signed in'}); run `codex login`. "
                "Codex GUI does not fall back to API billing.", 409, "not_subscription")

    # ---------- rate limits (display and history only) ----------

    def _on_global_notification(self, method: str, params: dict) -> None:
        if method != "account/rateLimits/updated":
            return
        parsed = parse_rate_limits(params)
        if parsed:
            self._set_limits(parsed, "update")

    def _set_limits(self, parsed: dict, reason: str, task_id: Optional[str] = None, force_history: bool = False) -> dict:
        prev = self._limits or {}
        if prev.get("limit_id") and parsed.get("limit_id") and prev["limit_id"] != parsed["limit_id"]:
            return prev  # another bucket's update; the dashboard shows the main one
        # Push updates carry only the windows: keep what only a full read tells us.
        for key in ("ordinary_usage_allowed", "available_resets"):
            if parsed.get(key) is None:
                parsed[key] = prev.get(key)
        parsed["fetched_at"] = now_iso()
        self._limits, self._limits_at = parsed, time.monotonic()
        if force_history or time.monotonic() - self._limits_history_at >= self.settings.rate_limit_history_seconds \
                or self._limits_history_at == 0.0:
            self._limits_history_at = time.monotonic()
            self._save_limits(parsed, reason, task_id)
        return parsed

    def _save_limits(self, parsed: dict, reason: str, task_id: Optional[str]) -> None:
        w = parsed["windows"] + [None, None]
        pick = lambda x, k: x[k] if x else None  # noqa: E731
        allowed = parsed.get("ordinary_usage_allowed")
        self.db.add_rate_limits(
            ts=now_iso(), reason=reason, task_id=task_id, plan_type=parsed.get("plan_type"),
            limit_id=parsed.get("limit_id"),
            primary_used_percent=pick(w[0], "used_percent"), primary_window_mins=pick(w[0], "duration_mins"),
            primary_resets_at=pick(w[0], "resets_at"),
            secondary_used_percent=pick(w[1], "used_percent"), secondary_window_mins=pick(w[1], "duration_mins"),
            secondary_resets_at=pick(w[1], "resets_at"),
            ordinary_usage_allowed=None if allowed is None else int(allowed),
            reached_type=parsed.get("reached_type"), available_resets=parsed.get("available_resets"))

    async def read_limits(self, reason: str = "poll", task_id: Optional[str] = None,
                          force_history: bool = False) -> Optional[dict]:
        """Ask Codex for the current rate limits. None if that is not possible right now. Never raises."""
        if not self.uses_app_server:
            return None
        try:
            result = await (await self.client()).request("account/rateLimits/read", timeout=20)
        except AppServerError:
            return None
        parsed = parse_rate_limits(result)
        return self._set_limits(parsed, reason, task_id, force_history) if parsed else None

    async def rate_limits(self, force: bool = False) -> dict:
        """The Codex Usage block of the dashboard. `available` is False when there is nothing to show."""
        if not self.uses_app_server:
            return {"available": False, "error": "rate limits need the app-server backend"}
        fresh = self._limits and time.monotonic() - self._limits_at < self.settings.rate_limit_cache_seconds
        if force or not fresh:
            await self.read_limits("poll")
        if not self._limits:
            return {"available": False, "error": "Codex did not report rate limits"}
        return {"available": True, **self._limits}

    # ---------- creation ----------

    async def create_task(self, *, repository: str, base_ref: str = "main", name: str = "", prompt: str,
                          model: str = "", reasoning_effort: str = "default",
                          auto_approval: bool = True, service_tier: str = "default", model_verbosity: str = "low",
                          web_search: bool = False, sandbox: str = "workspace-write",
                          adaptive_reasoning: bool = True, context_guard: bool = True,
                          writable_dirs: str = "", feature_flags: str = "") -> dict:
        prompt = prompt.strip()
        if not prompt:
            raise TaskError("prompt is required")
        name = name.strip() or (prompt.splitlines()[0][:60])
        model = model.strip()
        if model.startswith("-"):
            raise TaskError("invalid model name")
        if reasoning_effort != "default" and not EFFORT_RE.match(reasoning_effort):
            raise TaskError(f"invalid reasoning effort: {reasoning_effort}")
        service_tier = service_tier.strip() or "default"
        if not SERVICE_TIER_RE.match(service_tier):
            raise TaskError(f"invalid service tier: {service_tier}")
        if model_verbosity not in VERBOSITIES:
            raise TaskError(f"invalid output verbosity: {model_verbosity}")
        if sandbox not in SANDBOXES:
            raise TaskError(f"invalid sandbox: {sandbox} (allowed: {', '.join(SANDBOXES)})")
        dirs = [d.strip() for d in writable_dirs.splitlines() if d.strip()]
        for d in dirs:
            if not os.path.isabs(d) or "\n" in d or '"' in d:
                raise TaskError(f"additional writable dirs must be absolute paths: {d}")
        flags = feature_flags.replace(",", " ").split()
        for flag in flags:
            if not FEATURE_RE.match(flag):
                raise TaskError(f"invalid feature flag: {flag}")
        base_ref = base_ref.strip() or "main"

        repo_input = Path(repository.strip()).expanduser()
        if not repository.strip() or not repo_input.is_dir():
            raise TaskError(f"not a directory: {repository}")
        repo = await git.repo_toplevel(repo_input)
        if repo is None:
            raise TaskError(f"not a git repository: {repository}")
        base_sha = await git.resolve_commit(repo, base_ref)
        if base_sha is None:
            raise TaskError(f"base ref not found: {base_ref}")

        task_id = make_task_id()
        branch = branch_name(task_id, name)
        wt = worktree_path(self.settings.worktrees_dir, repo, task_id)
        # Several tasks may be created in the same repo at once; git's own locks (config, refs)
        # can make concurrent `worktree add` calls fail, so creation is serialized per repository.
        async with self._repo_locks.setdefault(repo, asyncio.Lock()):
            try:
                await git.create_worktree(repo, wt, branch, base_sha)
            except git.GitError as e:
                raise TaskError(f"git worktree add failed: {e}") from e

        task = self.db.create_task(
            id=task_id, name=name, repository=repo, worktree=str(wt), branch=branch,
            base_ref=base_ref, base_sha=base_sha, prompt=prompt, model=model,
            reasoning_effort=reasoning_effort, auto_approval=int(auto_approval),
            service_tier=service_tier, model_verbosity=model_verbosity, web_search_enabled=int(web_search),
            sandbox=sandbox, adaptive_reasoning=int(adaptive_reasoning), context_guard=int(context_guard),
            writable_dirs="\n".join(dirs), feature_flags=" ".join(flags), last_prompt=prompt,
            status="queued", git_summary="clean", created_at=now_iso(),
        )
        self.db.touch_repo(repo, now_iso())
        self._launch(task_id, _Turn(prompt))
        return task

    # ---------- further turns ----------

    def _require_idle_with_worktree(self, task: dict) -> None:
        if task["status"] in ACTIVE_STATUSES:
            raise TaskError(f"task is {task['status']}; send the next instruction when it has finished", 409, "active")
        self._require_worktree(task)

    async def send_instruction(self, task_id: str, prompt: str, reasoning_effort: Optional[str] = None) -> dict:
        """Another instruction for the task's existing Codex thread.

        Idle task: a new turn (`thread/resume` + `turn/start`, or `codex exec resume`). Running task (app-server): the
        text is added to the running turn with `turn/steer`. `reasoning_effort` is the explicit "Retry with ..."
        choice of the user; nothing here ever changes it on its own.
        """
        prompt = prompt.strip()
        if not prompt:
            raise TaskError("instruction is required")
        task = self.get(task_id)
        if reasoning_effort is not None and reasoning_effort != task["reasoning_effort"]:
            if reasoning_effort != "default" and not EFFORT_RE.match(reasoning_effort):
                raise TaskError(f"invalid reasoning effort: {reasoning_effort}")
        else:
            reasoning_effort = None
        if task["status"] in ACTIVE_STATUSES:
            return await self._steer(task, prompt)
        self._require_idle_with_worktree(task)
        thread = task["codex_thread_id"]
        if not thread:
            raise TaskError("this task has no recorded Codex session id; use Start New Session", 409, "no_session")
        fields = {}
        if reasoning_effort is not None:
            TaskLog.note(self.log_path(task_id),
                         f"reasoning effort changed {task['reasoning_effort']} -> {reasoning_effort} (requested by the user)")
            fields["reasoning_effort"] = reasoning_effort
        return self._begin_turn(task, _Turn(prompt, resume_thread=thread), **fields)

    async def _steer(self, task: dict, prompt: str) -> dict:
        """Add an instruction to the turn that is running now (turn/steer). The exec backend has no such thing."""
        if not self.uses_app_server:
            raise TaskError(f"task is {task['status']}; send the next instruction when it has finished", 409, "active")
        info = self._active_turns.get(task["id"])
        if not info or not info.get("turn") or task["status"] != "running":
            raise TaskError("the turn has not started yet; try again in a moment", 409, "active")
        try:
            await (await self.client()).request(
                "turn/steer", {"threadId": info["thread"], "expectedTurnId": info["turn"],
                               "input": [{"type": "text", "text": prompt}]}, timeout=30)
        except AppServerError as e:
            raise TaskError(f"could not add the instruction to the running turn: {e}", 409, "steer_failed") from e
        TaskLog.note(self.log_path(task["id"]), "additional instruction sent to the running turn (turn/steer):\n" +
                     prompt[:INSTRUCTION_LOG_CHARS])
        return self.db.update_task(task["id"], last_prompt=prompt)

    async def start_new_session(self, task_id: str, prompt: str) -> dict:
        """A fresh Codex session in the same worktree. Deliberately separate from send_instruction: it
        gives up the old session's conversation and the cached input that goes with it."""
        prompt = prompt.strip()
        if not prompt:
            raise TaskError("prompt is required")
        task = self.get(task_id)
        self._require_idle_with_worktree(task)
        return self._begin_turn(task, _Turn(prompt))

    async def compact(self, task_id: str) -> dict:
        """Compact the task's Codex thread (thread/compact/start). Task, worktree, branch and thread id stay as they are."""
        task = self.get(task_id)
        self._require_idle_with_worktree(task)
        if not self.uses_app_server:
            raise TaskError("compaction needs the app-server backend", 409, "unsupported")
        if not task["codex_thread_id"]:
            raise TaskError("this task has no Codex thread to compact", 409, "no_session")
        return self._begin_turn(task, _Turn("", resume_thread=task["codex_thread_id"], kind="compact"))

    def _begin_turn(self, task: dict, turn: _Turn, **fields) -> dict:
        # No await between the status checks of the caller and this transition, so two requests cannot both pass.
        if turn.kind == "turn":
            fields["last_prompt"] = turn.prompt
        updated = self.db.set_status(task["id"], "queued", pid=None, exit_code=None, finished_at=None,
                                     status_detail="", failure_source="", **fields)
        self._launch(task["id"], turn)
        return updated

    def _launch(self, task_id: str, turn: _Turn) -> None:
        job = asyncio.create_task(self._run(task_id, turn))
        self._jobs[task_id] = job
        job.add_done_callback(lambda j: self._forget_job(task_id, j))

    def _forget_job(self, task_id: str, job: asyncio.Task) -> None:
        if self._jobs.get(task_id) is job:  # a newer turn of the same task may already be registered
            del self._jobs[task_id]

    # ---------- running ----------

    async def _run(self, task_id: str, turn: _Turn) -> None:
        log = TaskLog(self.log_path(task_id))
        me = asyncio.current_task()
        try:
            if self._slots:
                await self._slots.acquire()
            try:
                if self.uses_app_server:
                    await self._run_app_server_turn(task_id, log, turn)
                else:
                    await self._run_process(task_id, log, turn)
            finally:
                if self._slots:
                    self._slots.release()
        except asyncio.CancelledError:
            raise  # stop() of a queued task; it already wrote the final status
        except Exception as e:  # never leave a task stuck in an active status
            log.add_system(f"internal error: {e!r}")
            if self.db.get_task(task_id)["status"] in ACTIVE_STATUSES:
                self.db.set_status(task_id, "failed", finished_at=now_iso(), status_detail=f"internal error: {e!r}"[:500])
        finally:
            log.close()
            self._overlap.discard(task_id)
            if self._jobs.get(task_id) is me:
                del self._jobs[task_id]
                self._procs.pop(task_id, None)
                self._active_turns.pop(task_id, None)
                self._stop_requested.discard(task_id)
                self._stop_deadline.pop(task_id, None)

    # ----- app-server backend -----

    def _note_overlap(self, task_id: str) -> None:
        """Remember that tasks ran together: their quota change cannot be told apart afterwards."""
        others = [t for t in self._active_turns if t != task_id]
        if others:
            self._overlap.update(others)
            self._overlap.add(task_id)

    def _thread_params(self, task: dict) -> dict:
        """thread/start and thread/resume carry the same settings, so a resumed thread never drifts."""
        params = {"cwd": task["worktree"], "serviceTier": task["service_tier"], "config": nested(task_config(task)),
                  **approval_params(task)}
        if task["model"]:
            params["model"] = task["model"]
        return params

    async def _run_app_server_turn(self, task_id: str, log: TaskLog, turn: _Turn) -> None:
        task = self.db.set_status(task_id, "starting")
        started_at = now_iso()
        compact = turn.kind == "compact"
        try:
            client = await self.client()
            await self._require_subscription(client)
        except (AppServerError, TaskError) as e:
            msg = f"cannot use Codex: {e}"
            log.add_system(msg)
            self.db.set_status(task_id, "failed", finished_at=now_iso(), status_detail=str(e)[:500])
            return

        before = await self.read_limits("turn_start", task_id, force_history=True)
        if quota_exhausted(before):
            # Codex says the included usage is exhausted: do not start a turn that cannot run, and do not retry.
            log.add_system("Codex reports that ordinary usage is not available (rate limit reached); not starting the turn.")
            self.db.set_status(task_id, "waiting-for-quota", finished_at=now_iso(),
                               status_detail=f"rate limit reached ({before.get('reached_type') or 'ordinary usage unavailable'})")
            return
        self.db.update_task(task_id, five_hour_used_before=before and before["five_hour_used"],
                            weekly_used_before=before and before["weekly_used"],
                            five_hour_used_after=None, weekly_used_after=None, quota_overlap=0)

        try:
            res = await client.request(
                "thread/resume" if turn.resume_thread else "thread/start",
                {**self._thread_params(task),
                 **({"threadId": turn.resume_thread, "excludeTurns": True} if turn.resume_thread else
                    {"serviceName": "codex-gui",
                     **({"developerInstructions": self.runner.instructions} if self.runner.instructions else {})})},
                timeout=60)
            thread = res["thread"]
            thread_id = thread["id"]
        except (AppServerError, KeyError, TypeError) as e:
            log.add_system(f"codex could not open the thread: {e}")
            self.db.set_status(task_id, "failed", finished_at=now_iso(), status_detail=f"thread: {e}"[:500])
            return
        turn.thread_id = thread_id
        model = res.get("model") or task["model"] or None
        if turn.resume_thread:
            log.add_system(f"turn: resuming Codex thread {thread_id}")
            if thread_id != turn.resume_thread:
                log.add_system(f"WARNING: asked to resume Codex thread {turn.resume_thread} but codex reported "
                               f"{thread_id}; the thread was NOT continued, so cached input is not reused")
        else:
            self.db.update_task(task_id, codex_thread_id=thread_id)
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
        info = self._active_turns[task_id] = {"thread": thread_id, "turn": None}
        self._note_overlap(task_id)
        try:
            if compact:
                await client.request("thread/compact/start", {"threadId": thread_id}, timeout=60)
            else:
                params = {"threadId": thread_id, "input": [{"type": "text", "text": turn.prompt}]}
                if task["reasoning_effort"] not in ("", "default"):
                    params["effort"] = task["reasoning_effort"]
                started = await client.request("turn/start", params, timeout=60)
                info["turn"] = started["turn"]["id"]
        except (AppServerError, KeyError, TypeError) as e:
            client.unsubscribe(thread_id)
            log.add_system(f"codex could not start the turn: {e}")
            self.db.set_status(task_id, "failed", finished_at=now_iso(), status_detail=f"turn/start: {e}"[:500])
            return
        self.db.set_status(task_id, "running", pid=None, started_at=started_at)
        if task_id in self._stop_requested:
            await self._interrupt(task_id)

        final, error, usage = None, None, None
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
                    raise AppServerError(params.get("reason", "app-server closed"))
                tid = params.get("turnId") or (params.get("turn") or {}).get("id")
                if info["turn"] and tid and tid != info["turn"]:
                    continue  # an event of some other turn of this thread
                if method == "turn/started" and not info["turn"] and tid:
                    info["turn"] = tid
                    if task_id in self._stop_requested:
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
                if method == "turn/completed":
                    final = params.get("turn") or {}
                    break
        except AppServerError as e:
            log.add_system(f"codex app-server failed during the turn: {e}")
            error = {"message": str(e)}
        finally:
            client.unsubscribe(thread_id)

        await self._finish_app_server_turn(task_id, log, turn, final, error, usage, started_at, model)

    async def _interrupt(self, task_id: str) -> None:
        info = self._active_turns.get(task_id)
        if not info or not info.get("turn"):
            return  # the turn id is not known yet; the loop interrupts as soon as it is
        self._stop_deadline.setdefault(task_id, time.monotonic() + self.settings.stop_grace_seconds)
        try:
            await (await self.client()).request("turn/interrupt", {"threadId": info["thread"], "turnId": info["turn"]}, timeout=15)
        except AppServerError:
            pass  # the deadline in the event loop ends the turn anyway

    async def _finish_app_server_turn(self, task_id, log, turn, final, error, usage, started_at, model) -> None:
        task = self.get(task_id)
        compact = turn.kind == "compact"
        turn_error = (final or {}).get("error") or error
        kind = error_kind(turn_error)
        if usage:
            self._store_turn(task_id, log, turn.thread_id or "", usage["total"], kind=turn.kind,
                             status=(final or {}).get("status", "interrupted"), turn_id=self._active_turns.get(task_id, {}).get("turn"),
                             started_at=started_at, model=model, effort=task["reasoning_effort"],
                             context=None if compact else usage)
        if compact:  # the size after compaction is only known from the next turn's first request
            self.db.update_task(task_id, context_tokens=None)

        after = await self.read_limits("turn_end", task_id, force_history=True)
        fields = {"finished_at": now_iso(), "exit_code": None}
        if after:
            fields.update(five_hour_used_after=after["five_hour_used"], weekly_used_after=after["weekly_used"])
        if task_id in self._overlap:
            fields["quota_overlap"] = 1

        status_text = (final or {}).get("status")
        if task_id in self._stop_requested:
            status = "interrupted" if self._shutting_down else "stopped"
        elif status_text == "completed":
            status = "completed"
        elif status_text == "interrupted":
            status = "stopped"
        else:
            # Quota is decided from what Codex said (error kind or limits snapshot), never from a failed run alone.
            if is_quota_error(kind) or quota_exhausted(after):
                status = "waiting-for-quota"
                fields["status_detail"] = f"Codex usage limit reached ({kind or (after or {}).get('reached_type') or 'unavailable'})"
            else:
                status = "failed"
                fields["status_detail"] = str((turn_error or {}).get("message") or "the turn failed")[:500]
                fields["failure_source"] = "codex" if final else "gui"
        log.add_system(f"turn finished: {status_text or 'no turn/completed'} -> {status}")
        await self.refresh_git_summary(task_id)
        # Last, and with nothing awaited afterwards: once the status is terminal a new turn may be started.
        self.db.set_status(task_id, status, **fields)

    # ----- exec backend -----

    async def _run_process(self, task_id: str, log: TaskLog, turn: _Turn) -> None:
        if turn.kind == "compact":
            raise TaskError("compaction needs the app-server backend", 409, "unsupported")
        task = self.db.set_status(task_id, "starting")
        try:
            proc = await self.runner.spawn(task, turn.resume_thread)
        except OSError as e:
            log.add_system(f"failed to start codex: {e}")
            self.db.set_status(task_id, "failed", finished_at=now_iso(), status_detail=f"failed to start codex: {e}"[:500])
            return
        self._procs[task_id] = proc
        if turn.resume_thread:
            log.add_system(f"turn: resuming Codex session {turn.resume_thread}")
        else:
            log.add_system("turn: starting a new Codex session")
        log.add_system("instruction:\n" + turn.prompt[:INSTRUCTION_LOG_CHARS] +
                       (f"\n… (+{len(turn.prompt) - INSTRUCTION_LOG_CHARS} chars)" if len(turn.prompt) > INSTRUCTION_LOG_CHARS else ""))
        log.add_system(f"started pid {proc.pid}: {' '.join(self.runner.build_command(task, turn.resume_thread))}")
        self.db.set_status(task_id, "running", pid=proc.pid, started_at=now_iso())
        if task_id in self._stop_requested:  # stop arrived while starting
            asyncio.create_task(terminate_process(proc, self.settings.stop_grace_seconds))

        async def pump(stream, write):
            while True:
                line = await stream.readline()
                if not line:
                    return
                write(line.decode(errors="replace"))

        async def feed_prompt():
            try:
                proc.stdin.write(turn.prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def on_stdout(line: str) -> None:
            self._on_event(task_id, log, turn, log.add_stdout(line)["event"])

        readers = [asyncio.create_task(pump(proc.stdout, on_stdout)),
                   asyncio.create_task(pump(proc.stderr, log.add_stderr)),
                   asyncio.create_task(feed_prompt())]
        refresher = asyncio.create_task(self._refresh_loop(task_id))
        try:
            code = await proc.wait()
            # A grandchild may keep the pipes open after codex exits; don't wait for it forever.
            await asyncio.wait(readers, timeout=READER_DRAIN_SECONDS)
        finally:
            refresher.cancel()
            for r in readers:
                r.cancel()

        if task_id in self._stop_requested:
            status = "interrupted" if self._shutting_down else "stopped"
        else:
            status = "completed" if code == 0 else "failed"
        log.add_system(f"process exited with code {code} -> {status}")
        if turn.thread_id is None:
            log.add_system("no thread.started event with a thread_id was seen in this process's output" +
                           ("" if turn.resume_thread else "; the Codex session id is unknown, so this task "
                            "cannot be resumed (only Start New Session is possible)"))
        # Last, and with nothing awaited afterwards: once the status is terminal a new turn may be started.
        await self.refresh_git_summary(task_id)
        self.db.set_status(task_id, status, exit_code=code, finished_at=now_iso(),
                           status_detail=f"codex exited with code {code}" if status == "failed" else "")

    # ---------- codex exec events: session id and token usage ----------

    def _on_event(self, task_id: str, log: TaskLog, turn: _Turn, event: Optional[dict]) -> None:
        """React to the parsed JSON events that matter here. Never raises: the log must keep flowing."""
        try:
            etype = event.get("type") if event else None
            if etype == "thread.started":
                self._on_thread_started(task_id, log, turn, event)
            elif etype == "turn.completed":
                self._on_turn_completed(task_id, log, turn, event)
        except Exception as e:
            log.add_system(f"could not process a codex event: {e!r}")

    def _on_thread_started(self, task_id: str, log: TaskLog, turn: _Turn, event: dict) -> None:
        thread_id = event.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            log.add_system(f"thread.started event has no usable thread_id: {json.dumps(event)[:300]}")
            return
        turn.thread_id = thread_id
        if turn.resume_thread:
            if thread_id != turn.resume_thread:
                log.add_system(f"WARNING: asked to resume Codex session {turn.resume_thread} but codex reported "
                               f"{thread_id}; the session was NOT continued, so cached input is not reused")
            return
        self.db.update_task(task_id, codex_thread_id=thread_id)
        log.add_system(f"codex session id: {thread_id}")

    def _on_turn_completed(self, task_id: str, log: TaskLog, turn: _Turn, event: dict) -> None:
        total = extract_usage(event)
        if total is None or not all(k in total for k in REQUIRED_KEYS):
            log.add_system(f"turn.completed without the expected usage fields; not recorded: {json.dumps(event)[:300]}")
            return
        task = self.get(task_id)
        self._store_turn(task_id, log, turn.thread_id or turn.resume_thread or "", total, kind="turn", status="completed",
                         started_at=task["started_at"], model=task["model"] or None, effort=task["reasoning_effort"])

    def _store_turn(self, task_id: str, log: TaskLog, thread_id: str, total: dict, *, kind: str, status: str,
                    started_at: Optional[str], model: Optional[str], effort: str, turn_id: Optional[str] = None,
                    context: Optional[dict] = None) -> None:
        """Save one turn's usage: the difference to the thread's previous running total, plus the new total."""
        if not all(k in total for k in REQUIRED_KEYS):
            log.add_system(f"usage without the expected fields; not recorded: {json.dumps(total)[:300]}")
            return
        turns = self.db.list_turns(task_id)
        previous = next((json.loads(t["total_json"]) for t in reversed(turns) if t["thread_id"] == thread_id), None)
        delta, not_cumulative = turn_delta(total, previous)
        if not_cumulative:
            log.add_system("usage went down since the previous turn of this thread; treating it as per-turn usage")
        threads = list(dict.fromkeys(t["thread_id"] for t in turns))
        session = threads.index(thread_id) + 1 if thread_id in threads else len(threads) + 1
        now = now_iso()
        self.db.add_turn(
            task_id=task_id, turn=(turns[-1]["turn"] + 1) if turns else 1, session=session, thread_id=thread_id,
            created_at=now, input_tokens=delta["input_tokens"], cached_input_tokens=delta["cached_input_tokens"],
            output_tokens=delta["output_tokens"], cache_write_input_tokens=delta.get("cache_write_input_tokens"),
            reasoning_output_tokens=delta.get("reasoning_output_tokens"), total_json=dumps(total),
            turn_id=turn_id, kind=kind, status=status, model=model, reasoning_effort=effort,
            started_at=started_at, finished_at=now,
            cache_hit_rate=cache_hit_rate(delta["input_tokens"], delta["cached_input_tokens"]),
        )
        fields = {"last_turn_at": now}
        if kind == "turn":
            fields.update(latest_input_tokens=delta["input_tokens"], latest_cached_input_tokens=delta["cached_input_tokens"],
                          latest_output_tokens=delta["output_tokens"])
        self.db.update_task(task_id, **fields)

    async def _refresh_loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.git_refresh_seconds)
            await self.refresh_git_summary(task_id)

    async def refresh_git_summary(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task and not task["worktree_removed"]:
            self.db.update_task(task_id, git_summary=await git.summary(task["worktree"], task["base_sha"]))

    # ---------- stop ----------

    async def stop(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task["status"] not in ACTIVE_STATUSES:
            raise TaskError(f"task is {task['status']}, not active", 409)
        if task["status"] == "queued":
            job = self._jobs.get(task_id)
            if job:
                job.cancel()
            return self.db.set_status(task_id, "stopped", finished_at=now_iso())
        self._stop_requested.add(task_id)
        if self.uses_app_server:
            await self._interrupt(task_id)  # no-op until the turn id is known; the turn loop repeats it then
            return task
        proc = self._procs.get(task_id)
        if proc:  # don't block the HTTP request for the SIGTERM -> SIGKILL grace period
            asyncio.create_task(terminate_process(proc, self.settings.stop_grace_seconds))
        return task

    async def shutdown(self) -> None:
        """Called when the GUI exits: running turns are stopped. Threads stay on disk and can be resumed later."""
        self._shutting_down = True
        for task_id in list(self._jobs):
            task = self.db.get_task(task_id)
            if task and task["status"] in ACTIVE_STATUSES:
                try:
                    await self.stop(task_id)
                except TaskError:
                    pass
        jobs = list(self._jobs.values())
        if jobs:
            await asyncio.wait(jobs, timeout=self.settings.stop_grace_seconds + READER_DRAIN_SECONDS + 5)
        if self._app_server is not None:
            await self._app_server.close()

    # ---------- restart recovery ----------

    def recover(self) -> list[str]:
        """Tasks left active in the DB by a previous GUI process cannot be re-attached: mark them interrupted."""
        fixed = []
        for task in self.db.list_tasks():
            if task["status"] not in ACTIVE_STATUSES:
                continue
            log = TaskLog(self.log_path(task["id"]))
            note = "GUI restarted while this task was active; marking it interrupted."
            if pid_is_codex(task["pid"]):
                note += f" (pid {task['pid']} still looks alive and was left running; it can no longer be tracked)"
            log.add_system(note)
            log.close()
            self.db.set_status(task["id"], "interrupted", finished_at=now_iso())
            fixed.append(task["id"])
        return fixed

    # ---------- git views and operations ----------

    def _require_worktree(self, task: dict) -> str:
        if task["worktree_removed"] or not os.path.isdir(task["worktree"]):
            raise TaskError("worktree no longer exists", 409, "no_worktree")
        return task["worktree"]

    def _require_inactive(self, task: dict) -> None:
        if task["status"] in ACTIVE_STATUSES:
            raise TaskError("task is still active; stop it first", 409)

    async def git_info(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task["worktree_removed"] or not os.path.isdir(task["worktree"]):
            return {"available": False, "branch": task["branch"]}
        wt, base = task["worktree"], task["base_sha"]
        try:
            status, stat, diff, log = await asyncio.gather(
                git.status_short(wt), git.diff_stat(wt, base), git.diff(wt, base), git.log_oneline(wt, 10))
        except git.GitError as e:
            return {"available": False, "branch": task["branch"], "error": str(e)}
        return {"available": True, "branch": task["branch"], "status": status,
                "diff_stat": stat, "diff": diff, "log": log}

    async def commit(self, task_id: str, message: str) -> str:
        task = self.get(task_id)
        self._require_inactive(task)
        wt = self._require_worktree(task)
        message = message.strip() or f"codex-gui: {task['name']}"
        try:
            out = await git.commit_all(wt, message)
        except git.GitError as e:
            raise TaskError(str(e), 409) from e
        await self.refresh_git_summary(task_id)
        return out

    async def push(self, task_id: str) -> str:
        task = self.get(task_id)
        self._require_inactive(task)
        wt = self._require_worktree(task)
        try:
            return await git.push(wt, task["branch"])
        except git.GitError as e:
            raise TaskError(str(e), 409) from e

    async def delete_worktree(self, task_id: str, force: bool = False) -> dict:
        task = self.get(task_id)
        self._require_inactive(task)
        if task["worktree_removed"]:
            raise TaskError("worktree already removed", 409)
        wt = task["worktree"]
        if os.path.isdir(wt):
            if not force and await git.is_dirty(wt):
                raise TaskError("worktree has uncommitted changes", 409, "dirty")
            try:
                await git.remove_worktree(task["repository"], wt, force=force)
            except git.GitError as e:
                raise TaskError(str(e), 409) from e
        else:
            await git.run_git(task["repository"], "worktree", "prune", check=False)
        # The branch is intentionally kept (and with it every commit); delete it separately.
        return self.db.update_task(task_id, worktree_removed=1, git_summary="worktree removed")

    async def delete_branch(self, task_id: str, force: bool = False) -> dict:
        task = self.get(task_id)
        self._require_inactive(task)
        if not task["worktree_removed"]:
            raise TaskError("delete the worktree first", 409)
        if task["branch_deleted"]:
            raise TaskError("branch already deleted", 409)
        try:
            await git.delete_branch(task["repository"], task["branch"], force=force)
        except git.GitError as e:
            code = "unmerged" if "not fully merged" in str(e) else ""
            raise TaskError(str(e), 409, code) from e
        return self.db.update_task(task_id, branch_deleted=1)

    # ---------- views ----------

    @staticmethod
    def present_turn(row: dict) -> dict:
        """A turns row for the API: only this turn's own figures plus the derived ones."""
        out = {k: row[k] for k in ("turn", "session", "thread_id", "created_at", "input_tokens", "cached_input_tokens",
                                   "output_tokens", "cache_write_input_tokens", "reasoning_output_tokens",
                                   "kind", "status", "model", "reasoning_effort", "started_at", "finished_at")}
        out["uncached_input_tokens"] = row["input_tokens"] - row["cached_input_tokens"]
        out["cache_hit_rate"] = cache_hit_rate(row["input_tokens"], row["cached_input_tokens"])
        return out

    def _present(self, task: dict, latest: Optional[dict]) -> dict:
        """The task plus the derived figures the UI shows (cache, context, quota, model, retry suggestion)."""
        task["cache_hit_rate"] = self.present_turn(latest)["cache_hit_rate"] if latest and latest["kind"] == "turn" else None
        task["effective_model"] = (latest or {}).get("model") or task["model"] or ""
        task["context"] = context_status(task["context_tokens"], task["context_window"],
                                         self.settings.context_warn_percent, bool(task["context_guard"]))
        task["backend"] = self.settings.backend
        return task

    def present_task(self, task: dict) -> dict:
        turns = self.db.list_turns(task["id"])
        latest = next((t for t in reversed(turns) if t["kind"] == "turn"), turns[-1] if turns else None)
        task = self._present(dict(task), latest)
        task["observed_quota"] = self._observed_quota(task)
        task["retry_suggestion"] = self._retry_suggestion(task)
        return task

    @staticmethod
    def _observed_quota(task: dict) -> Optional[dict]:
        pairs = {k: [task[f"{k}_used_before"], task[f"{k}_used_after"]] for k in ("five_hour", "weekly")}
        pairs = {k: v for k, v in pairs.items() if v[0] is not None and v[1] is not None}
        if not pairs:
            return None
        note = OBSERVED_NOTE + (" " + OBSERVED_OVERLAP_NOTE if task["quota_overlap"] else "")
        return {**pairs, "overlap": bool(task["quota_overlap"]), "note": note}

    @staticmethod
    def _retry_suggestion(task: dict) -> Optional[dict]:
        """Adaptive reasoning, as a suggestion only: after a turn Codex itself reported as failed (and not a quota
        stop), offer the next effort. It is never applied automatically, and never goes above high (xhigh, max and
        ultra are explicit choices only)."""
        if not task["adaptive_reasoning"] or task["status"] != "failed" or task["failure_source"] != "codex":
            return None
        effort = task["reasoning_effort"]
        if effort not in ESCALATION or effort == ESCALATION[-1]:
            return None
        return {"effort": ESCALATION[ESCALATION.index(effort) + 1], "reason": "the last turn failed"}

    def list_tasks_view(self) -> list[dict]:
        """Tasks for the dashboard, each with the cache hit rate (%) of its latest turn (None: nothing to show)."""
        latest = self.db.latest_turns()
        return [self._present(t, latest.get(t["id"])) for t in self.db.list_tasks()]

    def usage(self, task_id: str) -> dict:
        self.get(task_id)
        turns = [self.present_turn(r) for r in self.db.list_turns(task_id)]
        latest = next((t for t in reversed(turns) if t["kind"] == "turn"), None)
        return {"turns": turns, "latest": latest}

    def counts(self, tasks: list[dict]) -> dict:
        counts = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return counts

    @staticmethod
    def is_terminal(task: dict) -> bool:
        return task["status"] in TERMINAL_STATUSES
