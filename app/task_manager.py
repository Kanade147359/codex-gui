"""Creates tasks (branch + worktree), runs Codex turns in parallel, and keeps the DB in sync.

1 task = 1 branch = 1 worktree = 1 Codex thread. With the app-server backend (default) one shared `codex app-server`
hosts every task's thread: the first instruction is `thread/start` + `turn/start`, every later one `thread/resume` +
`turn/start` on the same thread (the history stays inside Codex; the GUI never re-sends it). The older `exec`
backend does the same with `codex exec` / `codex exec resume`.

Scheduling: a task that waits for other tasks is `waiting_dependencies`; every start goes through the atomic steps
waiting_dependencies/retry_wait -> queued (a conditional UPDATE) and queued -> claimed (another one), so a task is
started exactly once however often the scheduler looks at it. An unexpected stop is retried with a short recovery
instruction in the same worktree and the same Codex thread (see recovery.py); the user's Stop never is.
"""
import asyncio
import json
import logging
import os
import re
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from . import completion
from . import git_manager as git
from .appserver import CLOSED, AppServerClient, AppServerError
from .codex_login import CodexLogin
from .codex_runner import WEB_SEARCH_MODES, CodexRunner, approval_params, nested, task_config, terminate_process
from .attachments import AttachmentError, AttachmentStore
from .config import Settings
from .database import Database, DependencyError, ScheduledError
from .instructions import load_instructions
from .logstore import TaskLog, read_log
from .notifications import log_entry
from .procinfo import process_identity, same_process
from .recovery import NO_THREAD_NOTE, RECOVERY_PROMPT, Failure
from .scheduler import Scheduler
from . import ctx_config, ctx_guard, efficiency, recovery
from .ctx_manager import ContextFeatures, check_subdir_in_ref, task_cwd
from .usage import (
    REQUIRED_KEYS, cache_hit_rate, context_status, dumps, extract_usage, is_quota_error, parse_rate_limits,
    parse_token_usage, quota_exhausted, turn_delta,
)
from .models import (
    ACTIVE_STATUSES, BUSY_STATUSES, InvalidTransition, DEPENDENCY_FAILED_STATUSES, DEPENDENCY_POLICIES, EFFORT_RE, ESCALATION, SANDBOXES,
    SCHEDULED_WAITING, SERVICE_TIER_RE, TERMINAL_STATUSES, VERBOSITIES, branch_name, make_task_id, now_iso, timestamp, worktree_path,
)

logger = logging.getLogger(__name__)

READER_DRAIN_SECONDS = 5.0
INSTRUCTION_LOG_CHARS = 4000
RESUME_PROMPT = ("The previous turn was interrupted because the GUI stopped. Check the worktree (git status / git log) "
                 "to see what is already done, then continue the original task from where it stopped.")
FEATURE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
OBSERVED_NOTE = "Observed only; not an exact per-task cost."
OBSERVED_OVERLAP_NOTE = "Other tasks were running at the same time, so this cannot be attributed to this task."


RETRY_TRIGGERS = ("auto_retry", "manual_retry", "restart_recovery")
MAX_RETRIES_LIMIT = 10
DEPENDENCY_PHRASES = {"failed": "failed", "stopped": "was stopped", "blocked": "is blocked"}


@dataclass
class _Turn:
    """What one Codex turn (one process with `exec`, one turn/start with the app-server) is about."""
    prompt: str
    resume_thread: Optional[str] = None  # None: this turn starts a new Codex thread
    thread_id: Optional[str] = None      # from thread.started / thread/start
    kind: str = "turn"                   # "turn" | "compact"
    # What started this run: initial | instruction | scheduled_instruction | new_session | compact | auto_retry | manual_retry |
    # restart_recovery
    trigger: str = "instruction"
    # The speed REQUESTED for this turn only ("default" = Standard, "priority" = Fast); None = the task's own tier.
    service_tier: Optional[str] = None
    # Codex confirmed that the turn started (turn/started, turn.started or any item): its instruction is part of the thread,
    # so a retry only has to say "continue". Without that, the instruction may never have reached the thread.
    started: bool = False
    # A retry of a thread-creating turn that never started: if Codex never saved that thread, a new one is acceptable.
    fresh_ok: bool = False
    final_text: str = ""  # this turn's last completed assistant message; never tool output
    ignore_dependencies: bool = False  # explicit Run Anyway only, survives queue/restart
    attachment_ids: list[str] = field(default_factory=list)

    def message(self) -> dict:
        return dict(prompt=self.prompt, attachment_ids=self.attachment_ids, kind=self.trigger)

    def to_json(self) -> str:
        """Stored in tasks.pending_turn: the turn that is queued, running or to be retried survives a GUI restart."""
        return json.dumps({"prompt": self.prompt, "resume_thread": self.resume_thread, "thread_id": self.thread_id,
                           "kind": self.kind, "trigger": self.trigger, "service_tier": self.service_tier,
                           "started": self.started, "fresh_ok": self.fresh_ok, "ignore_dependencies": self.ignore_dependencies,
                           "attachment_ids": self.attachment_ids})

    @classmethod
    def from_json(cls, raw: Optional[str]) -> Optional["_Turn"]:
        try:
            d = json.loads(raw) if raw else None
        except ValueError:
            return None
        if not isinstance(d, dict) or not isinstance(d.get("prompt"), str):
            return None
        return cls(d["prompt"], d.get("resume_thread"), d.get("thread_id"), d.get("kind") or "turn",
                   d.get("trigger") or "instruction", d.get("service_tier"), bool(d.get("started")), bool(d.get("fresh_ok")),
                   ignore_dependencies=bool(d.get("ignore_dependencies")), attachment_ids=d.get("attachment_ids") or [])


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
        self.attachments = AttachmentStore(settings.home / "attachments", db)
        self.runner = runner or CodexRunner(settings.codex_bin, settings.subscription_only)
        self.runner.instructions = load_instructions(settings.instructions_path)
        self._app_server = app_server
        self._jobs: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._active_turns: dict[str, dict] = {}   # task id -> {"thread", "turn"} of the turn being run (app-server)
        self._completion_locks: dict[str, asyncio.Lock] = {}
        self._stop_requested: set[str] = set()
        self._stop_deadline: dict[str, float] = {}
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False
        # Identifies this GUI process in claims. Recovery assumes one GUI process per database (see recover()).
        self.instance_id = uuid.uuid4().hex
        self._attempts: dict[str, int] = {}        # task id -> id of the attempt row being run
        self._adopted: set[str] = set()            # running tasks of a previous GUI whose process is still alive
        self.scheduler = Scheduler(self.tick, settings.scheduler_interval_seconds)
        self.db.add_status_listener(self._status_changed)
        self._slots = asyncio.Semaphore(settings.max_concurrent) if settings.max_concurrent > 0 else None
        self._limits: Optional[dict] = None
        self._limits_at = 0.0
        self._limits_history_at = 0.0
        self._overlap: set[str] = set()
        self.ctx = ContextFeatures(db, settings)
        self._side_jobs: set[asyncio.Task] = set()
        self._guard_stops: dict[str, dict] = {}   # task id -> {"reason", "message"} of a stop the retry guard asked for
        self.codex_login = CodexLogin(self.client, settings.subscription_only)

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
                f"({'API key' if kind == 'apiKey' else 'not signed in'}); sign in from the dashboard (or run `codex login`). "
                "Codex GUI does not fall back to API billing.", 409, "not_subscription")

    # ---------- rate limits (display and history only) ----------

    def _on_global_notification(self, method: str, params: dict) -> None:
        self.codex_login.on_notification(method, params)
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
                          web_search: Union[bool, str] = "cached", sandbox: str = "workspace-write", network_access: bool = True,
                          adaptive_reasoning: bool = True, context_guard: bool = True,
                          writable_dirs: str = "", feature_flags: str = "",
                          depends_on=(), dependency_policy: str = "all_success",
                          auto_retry: Optional[bool] = None, max_retries: Optional[int] = None,
                          tool_output: str = "default", tool_output_limit: Optional[int] = None,
                          skills: str = "default", skills_budget: Optional[int] = None,
                          allow_subagents: bool = False, tool_profile: str = "full", cwd_subdir: str = "",
                          completion_contract: Optional[dict] = None, attachment_ids=()) -> dict:
        try:
            rules = completion.contract(completion_contract)
        except ValueError as e:
            raise TaskError(str(e)) from e
        prompt = prompt.strip()
        if not prompt and not attachment_ids:
            raise TaskError("prompt is required")
        await self._check_images(attachment_ids)
        name = name.strip() or (prompt.splitlines()[0][:60] if prompt else "Image task")
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
        # Codex's own default is web search ON (cached results). True meant live before there were modes.
        web_search_mode = {True: "live", False: "disabled"}.get(web_search, web_search)
        if web_search_mode not in WEB_SEARCH_MODES:
            raise TaskError(f"invalid web search mode: {web_search} (allowed: {', '.join(WEB_SEARCH_MODES)})")
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
        auto_retry = self.settings.default_auto_retry if auto_retry is None else bool(auto_retry)
        max_retries = self._check_max_retries(self.settings.default_max_retries if max_retries is None else max_retries)
        if dependency_policy not in DEPENDENCY_POLICIES:
            raise TaskError(f"invalid dependency policy: {dependency_policy} (allowed: {', '.join(DEPENDENCY_POLICIES)})")
        deps = self._check_dependency_ids(depends_on)

        repo_input = Path(repository.strip()).expanduser()
        if not repository.strip() or not repo_input.is_dir():
            raise TaskError(f"not a directory: {repository}")
        repo = await git.repo_toplevel(repo_input)
        if repo is None:
            raise TaskError(f"not a git repository: {repository}")
        base_sha = await git.resolve_commit(repo, base_ref)
        if base_sha is None:
            raise TaskError(f"base ref not found: {base_ref}")
        ctx_fields = await self._context_fields(
            repo, base_sha, tool_output=tool_output, tool_output_limit=tool_output_limit, skills=skills, skills_budget=skills_budget,
            allow_subagents=allow_subagents, tool_profile=tool_profile, cwd_subdir=cwd_subdir)

        task_id = make_task_id()
        branch = branch_name(task_id, name)
        wt = worktree_path(self.settings.worktrees_dir, repo, task_id)
        # A task that waits for others gets its worktree just before it runs (see _ensure_worktree): no idle worktrees
        # for tasks that may never start. An immediate task gets it now, as before.
        if not deps:
            # Several tasks may be created in the same repo at once; git's own locks (config, refs)
            # can make concurrent `worktree add` calls fail, so creation is serialized per repository.
            async with self._repo_locks.setdefault(repo, asyncio.Lock()):
                try:
                    await git.create_worktree(repo, wt, branch, base_sha)
                except git.GitError as e:
                    raise TaskError(f"git worktree add failed: {e}") from e

        try:
            self.db.create_task(
                depends_on=deps, message=_Turn(prompt, trigger="initial", attachment_ids=list(attachment_ids)).message(),
                id=task_id, name=name, repository=repo, worktree=str(wt), branch=branch,
                base_ref=base_ref, base_sha=base_sha, prompt=prompt, model=model,
                reasoning_effort=reasoning_effort, auto_approval=int(auto_approval),
                service_tier=service_tier, model_verbosity=model_verbosity, web_search_enabled=int(web_search_mode != "disabled"), web_search_mode=web_search_mode,
                sandbox=sandbox, network_access=int(network_access and sandbox == "workspace-write"),
                adaptive_reasoning=int(adaptive_reasoning), context_guard=int(context_guard),
                writable_dirs="\n".join(dirs), feature_flags=" ".join(flags), last_prompt=prompt,
                status="waiting_dependencies" if deps else "queued",
                git_summary="not started" if deps else "clean", worktree_pending=int(bool(deps)),
                dependency_policy=dependency_policy, auto_retry_enabled=int(auto_retry), max_retries=max_retries,
                pending_turn=_Turn(prompt, trigger="initial", attachment_ids=list(attachment_ids)).to_json(), created_at=now_iso(), **ctx_fields,
                completion_contract=json.dumps(rules),
            )
        except DependencyError as e:
            raise TaskError(str(e), 400, e.code) from e
        self.db.touch_repo(repo, now_iso())
        if ctx_fields["tool_profile"] != "full":
            self._verify_in_background(task_id)
        if deps:
            self._evaluate_one(task_id)  # the prerequisites may be done already (or already failed)
        else:
            self._dispatch(task_id)
        return self.db.get_task(task_id)

    # ---------- context efficiency (settings are frozen at creation; the logic lives in ctx_manager) ----------

    async def effective_config(self, cwd: str) -> Optional[dict]:
        """Codex's effective config for a directory (config/read), or None when the app-server cannot say."""
        if not self.uses_app_server:
            return None
        try:
            res = await (await self.client()).request("config/read", {"cwd": cwd, "includeLayers": False}, timeout=20)
        except AppServerError:
            return None
        cfg = res.get("config") if isinstance(res, dict) else None
        return cfg if isinstance(cfg, dict) else None

    async def _context_fields(self, repo: str, base_sha: str, *, tool_output, tool_output_limit, skills, skills_budget,
                              allow_subagents, tool_profile, cwd_subdir) -> dict:
        mcp = []
        if tool_profile == "minimal":
            cfg = await self.effective_config(repo)
            if cfg is None:
                raise TaskError("the Minimal tool profile needs Codex's MCP server list, which could not be read "
                                "(app-server unavailable)", 409, "config_unavailable")
            mcp = ctx_config.mcp_server_names(cfg)
        try:
            sub = await check_subdir_in_ref(repo, base_sha, cwd_subdir)
            return self.ctx.creation_fields(tool_output=tool_output, tool_output_limit=tool_output_limit, skills=skills,
                                            skills_budget=skills_budget, allow_subagents=allow_subagents, tool_profile=tool_profile,
                                            mcp_servers=mcp, cwd_subdir=sub)
        except ValueError as e:
            raise TaskError(str(e)) from e

    def _verify_in_background(self, task_id: str) -> None:
        """Measure what a non-Full tool profile really removes and keep the result on the task. Never blocks the task."""
        async def work():
            try:
                task = self.db.get_task(task_id)
                overrides = json.loads(task["tool_profile_config"] or "{}")
                mcp = [k.split(".")[1] for k, v in overrides.items() if k.startswith("mcp_servers.") and v is False]
                res = await self.ctx.verify_profile(task["repository"], task["tool_profile"], mcp)
                self.db.update_task(task_id, tool_profile_check=json.dumps(res))
            except Exception as e:  # a failed measurement only means "not verified"
                self.db.update_task(task_id, tool_profile_check=json.dumps({"ok": False, "verified": False, "error": f"{e}"[:200]}))
        try:
            job = asyncio.get_running_loop().create_task(work())
        except RuntimeError:
            return
        self._side_jobs.add(job)
        job.add_done_callback(self._side_jobs.discard)

    @staticmethod
    def _check_max_retries(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_RETRIES_LIMIT:
            raise TaskError(f"max retries must be a whole number from 0 to {MAX_RETRIES_LIMIT}")
        return value

    def _check_dependency_ids(self, depends_on) -> list[str]:
        ids = [str(d).strip() for d in (depends_on or []) if str(d).strip()]
        for i, dep in enumerate(ids):
            if dep in ids[:i]:
                raise TaskError(f"duplicate dependency: {dep}", 400, "duplicate")
            if self.db.get_task(dep) is None:
                raise TaskError(f"dependency task not found: {dep}", 400, "missing")
        return ids

    # ---------- further turns ----------

    def _require_idle_with_worktree(self, task: dict) -> None:
        self._require_completion_idle(task["id"])
        if task["status"] in BUSY_STATUSES:
            raise TaskError(f"task is {task['status']}; send the next instruction when it has finished", 409, "active")
        if task["status"] == "blocked":
            raise TaskError("task is blocked by a prerequisite; use Run Anyway or fix the dependency", 409, "blocked")
        self._require_worktree(task)

    async def _check_images(self, ids, resume=False) -> None:
        if not ids:
            return
        try:
            await asyncio.to_thread(self.attachments.resolve, ids)
            await self.runner.check_image_support(self.settings.backend, resume)
        except AttachmentError as e:
            raise TaskError(str(e), 400, "invalid_attachment") from e

    def _image_input(self, prompt: str, ids) -> list[dict]:
        return ([{"type": "text", "text": prompt}] if prompt else []) + [
            {"type": "localImage", "path": path} for path in self.attachments.paths(ids)]

    async def send_instruction(self, task_id: str, prompt: str, reasoning_effort: Optional[str] = None,
                               service_tier: Optional[str] = None, attachment_ids=()) -> dict:
        """Another instruction for the task's existing Codex thread.

        Idle task: a new turn (`thread/resume` + `turn/start`, or `codex exec resume`). Running task (app-server): the
        text is added to the running turn with `turn/steer`. `reasoning_effort` is the explicit "Retry with ..."
        choice of the user; nothing here ever changes it on its own. `service_tier` is the speed of THIS turn ("default" =
        Send Standard, "priority" = Send Fast; "standard" / "fast" are accepted too); omitted = the task's own speed.
        """
        prompt = prompt.strip()
        if not prompt and not attachment_ids:
            raise TaskError("instruction is required")
        await self._check_images(attachment_ids, resume=True)
        tier = self._turn_tier(service_tier)
        task = self.get(task_id)
        if reasoning_effort is not None and reasoning_effort != task["reasoning_effort"]:
            if reasoning_effort != "default" and not EFFORT_RE.match(reasoning_effort):
                raise TaskError(f"invalid reasoning effort: {reasoning_effort}")
        else:
            reasoning_effort = None
        if task["status"] in ACTIVE_STATUSES:
            return await self._steer(task, prompt, attachment_ids)
        self._require_idle_with_worktree(task)
        thread = task["codex_thread_id"]
        if not thread:
            raise TaskError("this task has no recorded Codex session id; use Start New Session", 409, "no_session")
        if ctx_guard.blocks_resend(task.get("stop_reason") or ""):
            # Sending again would resend the same huge context that just overflowed: the user must shrink it first.
            raise TaskError(ctx_guard.STOP_MESSAGES["context_window_exceeded"], 409, "context_overflow")
        fields = {}
        if reasoning_effort is not None:
            TaskLog.note(self.log_path(task_id),
                         f"reasoning effort changed {task['reasoning_effort']} -> {reasoning_effort} (requested by the user)")
            fields["reasoning_effort"] = reasoning_effort
        return self._begin_turn(task, _Turn(prompt, resume_thread=thread, service_tier=tier, attachment_ids=list(attachment_ids)), **fields)

    @staticmethod
    def _turn_tier(value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        tier = {"standard": "default", "fast": "priority"}.get(str(value).strip().lower(), str(value).strip().lower())
        if not SERVICE_TIER_RE.match(tier):
            raise TaskError(f"invalid service tier: {value}")
        return tier

    async def _steer(self, task: dict, prompt: str, attachment_ids=()) -> dict:
        """Add an instruction to the turn that is running now (turn/steer). The exec backend has no such thing."""
        if not self.uses_app_server:
            raise TaskError(f"task is {task['status']}; send the next instruction when it has finished", 409, "active")
        info = self._active_turns.get(task["id"])
        if not info or not info.get("turn") or task["status"] != "running":
            raise TaskError("the turn has not started yet; try again in a moment", 409, "active")
        try:
            await (await self.client()).request(
                "turn/steer", {"threadId": info["thread"], "expectedTurnId": info["turn"],
                               "input": self._image_input(prompt, attachment_ids)}, timeout=30)
        except AttachmentError as e:
            raise TaskError(str(e), 400, "invalid_attachment") from e
        except AppServerError as e:
            raise TaskError(f"could not add the instruction to the running turn: {e}", 409, "steer_failed") from e
        TaskLog.note(self.log_path(task["id"]), "additional instruction sent to the running turn (turn/steer):\n" +
                     prompt[:INSTRUCTION_LOG_CHARS])
        self.db.add_message(task["id"], prompt, attachment_ids)
        return self.db.update_task(task["id"], last_prompt=prompt)

    async def resume_interrupted(self, task_ids: Optional[list[str]] = None) -> dict:
        """Continue interrupted tasks in their existing Codex thread. Tasks without a thread id (or worktree) are
        skipped: they need Start New Session. Nothing is resumed that the caller did not select (None = all)."""
        resumed, skipped = [], []
        for task in self.db.list_tasks():
            if task["status"] != "interrupted" or (task_ids is not None and task["id"] not in task_ids):
                continue
            try:
                pending = self._in_flight_turn(task)
                if pending.attachment_ids and not pending.started:
                    await self.send_instruction(task["id"], pending.prompt, attachment_ids=pending.attachment_ids)
                else:
                    await self.send_instruction(task["id"], RESUME_PROMPT)
                resumed.append(task["id"])
            except TaskError as e:
                skipped.append({"id": task["id"], "reason": str(e)})
        return {"resumed": resumed, "skipped": skipped}

    async def start_new_session(self, task_id: str, prompt: str, attachment_ids=()) -> dict:
        """A fresh Codex session in the same worktree. Deliberately separate from send_instruction: it
        gives up the old session's conversation and the cached input that goes with it."""
        prompt = prompt.strip()
        if not prompt and not attachment_ids:
            raise TaskError("prompt is required")
        await self._check_images(attachment_ids)
        task = self.get(task_id)
        self._require_idle_with_worktree(task)
        return self._begin_turn(task, _Turn(prompt, trigger="new_session", attachment_ids=list(attachment_ids)), stop_reason="", long_context_ack="")

    async def compact(self, task_id: str) -> dict:
        """Compact the task's Codex thread (thread/compact/start). Task, worktree, branch and thread id stay as they are."""
        task = self.get(task_id)
        self._require_idle_with_worktree(task)
        if not self.uses_app_server:
            raise TaskError("compaction needs the app-server backend", 409, "unsupported")
        if not task["codex_thread_id"]:
            raise TaskError("this task has no Codex thread to compact", 409, "no_session")
        return self._begin_turn(task, _Turn("", resume_thread=task["codex_thread_id"], kind="compact", trigger="compact"),
                                stop_reason="", long_context_ack="")

    def _begin_turn(self, task: dict, turn: _Turn, **fields) -> dict:
        """A new unit of work for an idle task: queued with its turn recorded, then claimed and started at once."""
        # No await between the status checks of the caller and this transition, so two requests cannot both pass.
        try:
            updated = self.db.set_status(task["id"], "queued", message=turn.message() if turn.kind == "turn" else None,
                                         **self._queue_fields(turn, **fields))
        except InvalidTransition as e:
            raise TaskError(f"task changed while the instruction was being sent: {e}", 409, "active") from e
        self._dispatch(task["id"])
        return updated

    @staticmethod
    def _queue_fields(turn: _Turn, **fields) -> dict:
        """The task columns that make an idle task `queued` with `turn` as the work to run (also used by a scheduled instruction)."""
        if turn.kind == "turn":
            fields["last_prompt"] = turn.prompt
            fields.update(task_outcome="needs_review", outcome_reason="Completion has not been checked.",
                          outcome_source="unverified", completion_checked_at=None, completion_checks="[]",
                          semantic_result="", manual_override=0, manual_override_at=None, manual_override_by="")
            fields.update(evidence_result="UNKNOWN", evidence_reason="Completion has not been checked.",
                          approval_source="", approved_at=None)
            fields["completion_pending"] = 0
        return dict(pid=None, proc_identity=None, exit_code=None, finished_at=None, status_detail="", failure_source="",
                    pending_turn=turn.to_json(), claimed_by=None, claimed_at=None, retry_count=0, next_retry_at=None,
                    last_failure_kind="", last_failure_message="", last_exit_code=None, last_retry_at=None, **fields)

    def _pending_turn(self, task: dict) -> _Turn:
        """The turn a queued task is to run (rows from before pending_turn existed fall back to the last prompt)."""
        return _Turn.from_json(task["pending_turn"]) or _Turn(task["last_prompt"] or task["prompt"], trigger="initial")

    def _dispatch(self, task_id: str) -> bool:
        """Claim a queued task and start its job. The claim is an atomic UPDATE: of any number of callers (a request, the
        scheduler, a status listener) exactly one wins, so a task is never started twice. False if this call did not start it."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False  # not inside the event loop (a plain DB call): the scheduler's next tick starts it
        if self._shutting_down:
            return False
        task = self.db.get_task(task_id)
        if task is None or task["status"] != "queued" or task["claimed_by"]:
            return False
        turn = self._pending_turn(task)
        check_deps = turn.trigger == "initial" and not turn.ignore_dependencies
        if not self.db.claim_queued(task_id, self.instance_id, check_dependencies=check_deps):
            if check_deps and not self.db.dependencies_ready(task_id):
                self.db.transition(task_id, "queued", "waiting_dependencies", claimed_by=None, claimed_at=None,
                                   status_detail="Waiting for dependencies to succeed.")
            return False
        self._launch(task_id, turn)
        return True

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
            self._begin_attempt(task_id, turn)
            if self._slots:
                await self._slots.acquire()
            try:
                if self._pause_for_dependencies(task_id, turn, log):
                    return
                self.db.set_status(task_id, "starting")
                if await self._prepare(task_id, log, turn):
                    if self._pause_for_dependencies(task_id, turn, log):
                        return
                    if self.uses_app_server:
                        await self._run_app_server_turn(task_id, log, turn)
                    else:
                        await self._run_process(task_id, log, turn)
            finally:
                if self._slots:
                    self._slots.release()
        except asyncio.CancelledError:
            self._end_attempt(task_id, "stopped", turn)  # stop() of a queued task; it already wrote the final status
            raise
        except AttachmentError as e:
            current = self.db.get_task(task_id)
            if current and current["status"] in ACTIVE_STATUSES:
                self._settle(task_id, log, turn, "failed", failure=Failure("invalid_attachment", recovery.NON_RETRYABLE, str(e)))
        except Exception as e:  # never leave a task stuck in an active status
            log.add_system(f"internal error: {e!r}")
            current = self.db.get_task(task_id)
            if current and current["status"] in ACTIVE_STATUSES:
                if current["completion_pending"]:
                    self._settle(task_id, log, turn, "completed", exit_code=current["exit_code"],
                                 task_outcome="needs_review", outcome_source="gate",
                                 outcome_reason=f"Completion interrupted: {e!r}"[:1000])
                else:
                    self._settle(task_id, log, turn, "failed",
                                 failure=Failure("internal_error", recovery.UNKNOWN, f"internal error: {e!r}"[:500]))
            else:
                self._end_attempt(task_id, "failed", turn)
        finally:
            log.close()
            self._overlap.discard(task_id)
            if self._jobs.get(task_id) is me:
                del self._jobs[task_id]
                self._procs.pop(task_id, None)
                self._active_turns.pop(task_id, None)
                self._stop_requested.discard(task_id)
                self._guard_stops.pop(task_id, None)
                self._stop_deadline.pop(task_id, None)
                self._attempts.pop(task_id, None)

    def _pause_for_dependencies(self, task_id: str, turn: _Turn, log: TaskLog) -> bool:
        if turn.trigger != "initial" or turn.ignore_dependencies or self.db.dependencies_ready(task_id):
            return False
        task = self.get(task_id)
        if self.db.transition(task_id, task["status"], "waiting_dependencies", claimed_by=None, claimed_at=None,
                              status_detail="Waiting for dependencies to succeed."):
            log.add_system("dependency success was revoked before execution; waiting again")
            self._end_attempt(task_id, "dependency_wait", turn)
        return True

    async def _prepare(self, task_id: str, log: TaskLog, turn: _Turn) -> bool:
        """Before Codex is started: the worktree must exist (a waiting task gets it now), and before a retry its state is
        recorded. Nothing is reset, cleaned or checked out. False if the run cannot go on (the task is settled already)."""
        try:
            await self._check_images(turn.attachment_ids, resume=bool(turn.resume_thread))
        except TaskError as e:
            self._settle(task_id, log, turn, "failed", failure=Failure("invalid_attachment", recovery.NON_RETRYABLE, str(e)))
            return False
        task = self.get(task_id)
        failure = await self._ensure_worktree(task, log)
        if failure:
            log.add_system(f"cannot start: {failure.message}")
            self._settle(task_id, log, turn, "failed", failure=failure)
            return False
        if turn.trigger in RETRY_TRIGGERS:
            task = self.get(task_id)
            log.add_system(f"{'automatic ' if turn.trigger == 'auto_retry' else ''}retry "
                           f"({task['retry_count']}/{task['max_retries']}, {turn.trigger.replace('_', ' ')}) in the existing worktree "
                           f"{task['worktree']} on branch {task['branch']}")
            if not turn.resume_thread and turn.kind == "turn":
                log.add_system(NO_THREAD_NOTE)
            elif turn.kind == "turn" and turn.prompt != RECOVERY_PROMPT:
                log.add_system("The interrupted turn had not started (Codex never confirmed it), so its instruction may not be in "
                               "the thread; nothing can have been done yet, so the same instruction is sent again on the same thread.")
            await self._record_git_state(task, log)
        return True

    async def _ensure_worktree(self, task: dict, log: TaskLog) -> Optional[Failure]:
        """Create the worktree of a task that waited for its dependencies. An existing one that has vanished is NOT recreated."""
        if task["worktree_pending"]:
            repo, wt = task["repository"], Path(task["worktree"])
            async with self._repo_locks.setdefault(repo, asyncio.Lock()):
                try:
                    sha = await git.resolve_commit(repo, task["base_ref"])  # the base as it is now, not as it was at creation
                    if sha is None:
                        return Failure("git_invalid", recovery.NON_RETRYABLE, f"base ref not found: {task['base_ref']}")
                    await git.create_worktree(repo, wt, task["branch"], sha)
                except git.GitError as e:
                    return recovery.classify_git_error(str(e))
            self.db.update_task(task["id"], worktree_pending=0, base_sha=sha, git_summary="clean")
            log.add_system(f"worktree created: {wt} (branch {task['branch']}, base {task['base_ref']} {sha[:10]})")
            return None
        if task["worktree_removed"] or not os.path.isdir(task["worktree"]):
            return Failure("worktree_missing", recovery.NON_RETRYABLE,
                           "the worktree no longer exists; it is not recreated automatically")
        return None

    async def _record_git_state(self, task: dict, log: TaskLog) -> None:
        """git status / HEAD / push state, written to the log and the attempt before a retry. Read-only."""
        try:
            snap = await git.snapshot(task["worktree"])
        except git.GitError as e:
            log.add_system(f"could not read the git state before the retry: {e}")
            return
        push = ("no upstream to compare with" if snap["unpushed"] is None
                else "everything is pushed" if snap["unpushed"] == 0 else f"{snap['unpushed']} commit(s) not pushed")
        log.add_system(f"git state before the retry (not modified): HEAD {snap['head'] or '(none)'}; {push}\n{snap['status']}")
        aid = self._attempts.get(task["id"])
        if aid:
            self.db.update_attempt(aid, git_head=snap["head"], git_status=snap["status"], unpushed=snap["unpushed"])

    # ----- attempts: one row per run, kept apart from the token-usage turns -----

    def _begin_attempt(self, task_id: str, turn: _Turn) -> None:
        task = self.get(task_id)
        self._attempts[task_id] = self.db.start_attempt(
            task_id=task_id, trigger_kind=turn.trigger, started_at=now_iso(), was_resume=int(bool(turn.resume_thread)),
            service_tier=turn.service_tier or task["service_tier"], reasoning_effort=task["reasoning_effort"],
            codex_thread_id=turn.resume_thread)

    def _end_attempt(self, task_id: str, result: str, turn: Optional[_Turn] = None, *, exit_code: Optional[int] = None,
                     failure: Optional[Failure] = None) -> None:
        aid = self._attempts.pop(task_id, None)
        if aid is None:  # an attempt of a GUI process that is gone
            open_attempt = self.db.open_attempt(task_id)
            aid = open_attempt["id"] if open_attempt else None
        if aid is None:
            return
        fields = {"finished_at": now_iso(), "result": result, "exit_code": exit_code}
        if failure:
            fields.update(failure_kind=failure.kind, failure_message=failure.message)
        thread = turn and (turn.thread_id or turn.resume_thread)
        if thread:
            fields["codex_thread_id"] = thread
        self.db.update_attempt(aid, **fields)

    # ----- ending a run: the one place that decides between done, failed, quota and "try again" -----

    def _settle(self, task_id: str, log: TaskLog, turn: _Turn, status: str, *, failure: Optional[Failure] = None,
                exit_code: Optional[int] = None, retry_trigger: str = "auto_retry", **fields) -> str:
        """Write the final status of a run. A failure that is retryable (or unknown) and still within the task's limit
        becomes retry_wait instead of failed. Sync and last: once the status is terminal a new turn may be started.
        Returns the status that was set."""
        task = self.get(task_id)
        if status == "failed" and task_id in self._stop_requested:  # Stop wins over whatever the stop made fail: never retried
            status, failure = ("interrupted" if self._shutting_down else "stopped"), None
        fields.setdefault("finished_at", now_iso())
        fields.setdefault("exit_code", exit_code)
        fields.setdefault("completion_pending", 0)
        result = {"completed": "completed", "stopped": "stopped", "interrupted": "shutdown",
                  "waiting-for-quota": "quota", "failed": "failed"}[status]
        if status == "completed":
            fields.update(next_retry_at=None, pending_turn=None, status_detail=fields.get("status_detail", ""))
        else:
            fields.update(task_outcome="incomplete", outcome_source="execution",
                          outcome_reason=(failure.message if failure else f"Execution {status}.")[:1000],
                          manual_override=0, manual_override_at=None, manual_override_by="")
            fields.update(evidence_result="UNKNOWN", evidence_reason="Execution did not complete normally.",
                          approval_source="", approved_at=None)
        if failure:
            fields.update(last_failure_kind=failure.kind, last_failure_message=failure.message[:500], last_exit_code=exit_code)
            log.add_system(failure.message if failure.category == recovery.QUOTA else
                           f"unexpected stop [{failure.kind}, {failure.category}]: {failure.message}")
        if status == "failed" and failure:
            decision = recovery.decide(failure, enabled=bool(task["auto_retry_enabled"]),
                                       retry_count=task["retry_count"], max_retries=task["max_retries"])
            if decision.action == "quota":
                status, result = "waiting-for-quota", "quota"
                fields["status_detail"] = failure.message[:500]
            elif decision.action == "retry" and not self._shutting_down:
                n = task["retry_count"] + 1
                delay = recovery.backoff_seconds(self.settings.retry_backoff_seconds, n)
                retry = self._recovery_turn(turn, retry_trigger)
                status, result = "retry_wait", "interrupted"
                fields.update(retry_count=n, next_retry_at=timestamp(delay), pending_turn=retry.to_json(), pid=None,
                              proc_identity=None, finished_at=None, status_detail=f"Retry {n}/{task['max_retries']} in {delay:g}s: {failure.message}"[:500])
                thread = retry.resume_thread
                log.add_system(f"automatic retry {n}/{task['max_retries']} in {delay:g}s ({failure.kind}); "
                               + (f"will resume the same Codex thread {thread} in the same worktree" if thread else
                                  "no Codex thread id exists yet, so the retry will start a new thread in the same worktree"))
            else:
                detail = failure.message
                if decision.reason == "retry_limit":
                    detail += f" (automatic retry limit reached: {task['retry_count']}/{task['max_retries']})"
                    log.add_system(f"automatic retry limit reached ({task['retry_count']}/{task['max_retries']}); giving up")
                elif decision.reason == "auto_retry_disabled" and not failure.category == recovery.NON_RETRYABLE:
                    log.add_system("automatic retry is off for this task")
                fields["status_detail"] = detail[:500]
        self._end_attempt(task_id, result, turn, exit_code=exit_code, failure=failure)
        return self.db.set_status(task_id, status, **fields)["status"]

    @staticmethod
    def _recovery_turn(turn: _Turn, trigger: str) -> _Turn:
        """The turn that continues `turn` after an unexpected stop.

        - The turn started and there is a Codex thread: the fixed recovery instruction on that same thread. The original
          instruction is never sent again: it is in the thread already, and sending it could repeat work that was done.
        - There is a thread but Codex never confirmed that the turn started: the instruction may not have reached the thread
          (a real run showed a resumed thread that had never seen it), and nothing can have been done yet, so the same
          instruction is sent again on the same thread.
        - No thread (the process died before thread.started): the same instruction again, as a new thread in the same worktree.
        """
        thread = turn.thread_id or turn.resume_thread
        if turn.kind == "compact":
            return _Turn("", resume_thread=thread, kind="compact", trigger=trigger, service_tier=turn.service_tier)
        if thread and (turn.started or turn.prompt == RECOVERY_PROMPT):  # (a recovery turn only exists after a started one)
            return _Turn(RECOVERY_PROMPT, resume_thread=thread, trigger=trigger, service_tier=turn.service_tier)
        if thread:
            return _Turn(turn.prompt, resume_thread=thread, trigger=trigger, service_tier=turn.service_tier,
                         fresh_ok=turn.fresh_ok or turn.resume_thread is None, attachment_ids=list(turn.attachment_ids))
        return _Turn(turn.prompt, trigger=trigger, service_tier=turn.service_tier, attachment_ids=list(turn.attachment_ids))

    def _mark_started(self, task_id: str, turn: _Turn) -> None:
        """Codex confirmed the turn started: remember it, so that a retry knows its instruction is in the thread."""
        if not turn.started:
            turn.started = True
            self.db.update_task(task_id, pending_turn=turn.to_json())

    def _in_flight_turn(self, task: dict) -> _Turn:
        """The turn a task was running (or was to run) when it stopped, as stored on the task. A row from before
        pending_turn existed falls back to the task's own thread, else its last instruction."""
        return _Turn.from_json(task["pending_turn"]) or _Turn(task["last_prompt"] or task["prompt"], thread_id=task["codex_thread_id"])

    def _turn_of(self, task: dict, trigger: Optional[str] = None) -> _Turn:
        """The recovery turn for a task that is not running in this process (manual retry, a due timer). Without a
        `trigger` the one stored with the retry stays (auto_retry, or restart_recovery for a task found orphaned)."""
        pending = self._in_flight_turn(task)
        return self._recovery_turn(pending, trigger or pending.trigger)

    def _persist_thread(self, task_id: str, turn: _Turn, thread_id: str) -> None:
        """A turn that started a NEW thread: remember its id on the task and in the pending turn (a retry resumes it)."""
        turn.thread_id = thread_id
        self.db.update_task(task_id, codex_thread_id=thread_id, pending_turn=turn.to_json())

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

    # ----- exec backend -----

    async def _run_process(self, task_id: str, log: TaskLog, turn: _Turn) -> None:
        if turn.kind == "compact":
            raise TaskError("compaction needs the app-server backend", 409, "unsupported")
        task = self.get(task_id)
        task = {**task, "image_paths": self.attachments.paths(turn.attachment_ids),
                "service_tier": turn.service_tier or task["service_tier"]}
        try:
            proc = await self.runner.spawn(task, turn.resume_thread)
        except OSError as e:
            log.add_system(f"failed to start codex: {e}")
            permanent = isinstance(e, (FileNotFoundError, PermissionError, NotADirectoryError))
            failure = Failure("codex_unavailable" if permanent else "transient_io",
                              recovery.NON_RETRYABLE if permanent else recovery.RETRYABLE, f"failed to start codex: {e}"[:500])
            self._settle(task_id, log, turn, "failed", failure=failure)
            return
        self._procs[task_id] = proc
        if turn.resume_thread:
            log.add_system(f"turn: resuming Codex session {turn.resume_thread}")
        else:
            log.add_system("turn: starting a new Codex session")
        log.add_system("instruction:\n" + turn.prompt[:INSTRUCTION_LOG_CHARS] +
                       (f"\n… (+{len(turn.prompt) - INSTRUCTION_LOG_CHARS} chars)" if len(turn.prompt) > INSTRUCTION_LOG_CHARS else ""))
        log.add_system(f"started pid {proc.pid}: {' '.join(self.runner.build_command(task, turn.resume_thread))}")
        self.db.set_status(task_id, "running", pid=proc.pid, proc_identity=process_identity(proc.pid), started_at=now_iso())
        if task_id in self._stop_requested:  # stop arrived while starting
            asyncio.create_task(terminate_process(proc, self.settings.stop_grace_seconds))

        stderr_tail: deque = deque(maxlen=20)
        last_error: list = []  # the newest turn.failed / error message of the process

        async def pump(stream, write):
            while True:
                line = await stream.readline()
                if not line:
                    return
                write(line.decode(errors="replace"))

        async def feed_prompt():
            try:
                prompt = turn.prompt + "\n\n" + completion.RESULT_INSTRUCTION
                rules = json.loads(task["completion_contract"] or "{}")
                if rules:
                    prompt += "\nCompletion contract: " + json.dumps(rules)
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def on_stdout(line: str) -> None:
            event = log.add_stdout(line)["event"]
            self._on_event(task_id, log, turn, event)
            if isinstance(event, dict) and event.get("type") in ("turn.failed", "error"):
                err = event.get("error")
                message = err.get("message") if isinstance(err, dict) else event.get("message") or err
                if message:
                    last_error[:] = [str(message)]

        def on_stderr(line: str) -> None:
            if line.strip():
                stderr_tail.append(line.strip())
            log.add_stderr(line)

        readers = [asyncio.create_task(pump(proc.stdout, on_stdout)),
                   asyncio.create_task(pump(proc.stderr, on_stderr)),
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

        failure = None
        if task_id in self._stop_requested:
            status = "interrupted" if self._shutting_down else "stopped"
        elif code == 0:
            status = "completed"
            self._record_completion_pending(task_id, turn.final_text, exit_code=0)
        else:
            status = "failed"
            failure = recovery.classify_exit(code, last_error[0] if last_error else "\n".join(stderr_tail))
        log.add_system(f"process exited with code {code} -> {status}")
        if turn.thread_id is None:
            log.add_system("no thread.started event with a thread_id was seen in this process's output" +
                           ("" if turn.resume_thread else "; the Codex session id is unknown, so this task "
                            "cannot be resumed (only Start New Session is possible)"))
        # Last, and with nothing awaited afterwards: once the status is terminal a new turn may be started.
        await self.refresh_git_summary(task_id)
        fields = await self._completion_fields(task_id, turn.final_text) if status == "completed" else {}
        if task_id in self._stop_requested:
            status = "interrupted" if self._shutting_down else "stopped"
            fields.update(task_outcome="incomplete", outcome_reason="Stopped before completion was accepted.", outcome_source="gate")
        self._settle(task_id, log, turn, status, failure=failure, exit_code=code, **fields)

    # ---------- completion gate ----------

    def completion_approval_settings(self) -> dict:
        return {"auto_approve_verified_success": self.db.get_settings("completion.").get(
            "completion.auto_approve_verified_success", "1") == "1"}

    def set_completion_approval(self, enabled: bool) -> dict:
        # Takes effect for future checks; changing the setting never re-runs historical tasks.
        self.db.set_setting("completion.auto_approve_verified_success", "1" if enabled else "0")
        return self.completion_approval_settings()

    def _require_completion_idle(self, task_id: str) -> None:
        lock = self._completion_locks.get(task_id)
        task = self.db.get_task(task_id)
        if (lock and lock.locked()) or (task and task["completion_pending"]):
            raise TaskError("completion checks are running; try again after they finish", 409, "completion_busy")

    def _record_completion_pending(self, task_id: str, text: str, **fields) -> None:
        result = completion.parse_result(text)
        self.db.update_task(task_id, completion_pending=1,
                            semantic_result=json.dumps(result) if result else "", **fields)

    async def _completion_fields(self, task_id: str, text: str) -> dict:
        try:
            fields = await completion.check(self.get(task_id), text, self.settings.home)
            return completion.approve_verified(fields, self.completion_approval_settings()["auto_approve_verified_success"])
        except Exception as e:
            # Check infrastructure errors are semantic uncertainty, never automatic process retries.
            return dict(task_outcome="needs_review", outcome_reason=f"Completion checks unavailable: {e}"[:1000],
                        outcome_source="gate", completion_checked_at=now_iso(),
                        completion_checks=json.dumps([{"name": "completion checks", "status": "fail", "reason": str(e)[:500]}]),
                        semantic_result=json.dumps(completion.parse_result(text)) if completion.parse_result(text) else "",
                        manual_override=0, manual_override_at=None, manual_override_by="", completion_pending=0,
                        evidence_result="UNKNOWN", evidence_reason=str(e)[:1000], approval_source="", approved_at=None)

    async def rerun_completion(self, task_id: str) -> dict:
        self._require_completion_idle(task_id)
        task = self.get(task_id)
        if task["status"] != "completed":
            raise TaskError("completion checks require a normally completed execution", 409)
        self._require_worktree(task)
        async with self._completion_locks.setdefault(task_id, asyncio.Lock()):
            # Revoke any old success before waiting on commands, so the scheduler cannot release more work.
            self.db.update_task(task_id, task_outcome="needs_review", outcome_source="gate",
                                outcome_reason="Completion checks are running.", manual_override=0,
                                manual_override_at=None, manual_override_by="", completion_pending=1,
                                evidence_result="UNKNOWN", evidence_reason="Completion checks are running.",
                                approval_source="", approved_at=None)
            try:
                fields = await self._completion_fields(task_id, task["semantic_result"])
                self.db.update_task(task_id, **fields)
            finally:
                self.db.update_task(task_id, completion_pending=0)
                self.scheduler.wake()
        self._evaluate_dependents(task_id)
        return self.present_task(self.get(task_id))

    def override_completion(self, task_id: str, outcome: str, reason: str, confirm: bool) -> dict:
        import getpass
        self._require_completion_idle(task_id)
        task = self.get(task_id)
        if task["status"] != "completed":
            raise TaskError("manual outcome requires a normally completed execution", 409)
        if outcome == "success" and not confirm:
            raise TaskError("confirm the manual success override; this releases dependent tasks", 409, "confirmation_required")
        reason = reason.strip()
        if not reason:
            raise TaskError("a reason is required for a manual outcome")
        try:
            # This local, single-user service records its OS account; clients cannot impersonate another actor.
            self.db.override_completion(task_id, outcome, reason[:1000], getpass.getuser())
        except ValueError as e:
            raise TaskError(str(e), 409) from e
        self.scheduler.wake()
        return self.present_task(self.get(task_id))

    def set_completion_contract(self, task_id: str, value: dict) -> dict:
        self._require_completion_idle(task_id)
        task = self.get(task_id)
        if task["status"] in ACTIVE_STATUSES or task["status"] == "retry_wait":
            raise TaskError("completion contract cannot change during execution", 409)
        try:
            rules = completion.contract(value)
        except ValueError as e:
            raise TaskError(str(e)) from e
        self.db.update_task(task_id, completion_contract=json.dumps(rules), task_outcome="needs_review",
                            outcome_source="gate", outcome_reason="Completion contract changed; re-run completion checks.",
                            completion_checked_at=None, completion_checks="[]", manual_override=0,
                            manual_override_at=None, manual_override_by="", approval_source="", approved_at=None,
                            evidence_result="UNKNOWN", evidence_reason="Completion contract changed; re-run completion checks.")
        self.scheduler.wake()
        return self.present_task(self.get(task_id))

    async def cancel_dependents(self, task_id: str) -> dict:
        self.get(task_id)
        cancelled, seen, pending = [], {task_id}, list(self.db.dependents_of(task_id))
        while pending:
            child = pending.pop()
            if child in seen:
                continue
            seen.add(child)
            pending.extend(self.db.dependents_of(child))
            task = self.get(child)
            if task["status"] in ("waiting_dependencies", "blocked"):
                await self.stop(child)
                cancelled.append(child)
        return {"cancelled": cancelled}

    # ---------- codex exec events: session id and token usage ----------

    def _on_event(self, task_id: str, log: TaskLog, turn: _Turn, event: Optional[dict]) -> None:
        """React to the parsed JSON events that matter here. Never raises: the log must keep flowing."""
        try:
            etype = event.get("type") if event else None
            if etype in ("turn.started", "turn.completed") or (etype or "").startswith("item."):
                self._mark_started(task_id, turn)
            if etype == "thread.started":
                self._on_thread_started(task_id, log, turn, event)
            elif etype == "turn.completed":
                self._on_turn_completed(task_id, log, turn, event)
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("phase") in (None, "final_answer"):
                    turn.final_text = item.get("text") if isinstance(item.get("text"), str) else ""
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
        self._persist_thread(task_id, turn, thread_id)
        log.add_system(f"codex session id: {thread_id}")

    def _on_turn_completed(self, task_id: str, log: TaskLog, turn: _Turn, event: dict) -> None:
        total = extract_usage(event)
        if total is None or not all(k in total for k in REQUIRED_KEYS):
            log.add_system(f"turn.completed without the expected usage fields; not recorded: {json.dumps(event)[:300]}")
            return
        task = self.get(task_id)
        self._store_turn(task_id, log, turn.thread_id or turn.resume_thread or "", total, kind="turn", status="completed",
                         started_at=task["started_at"], model=task["model"] or None, effort=task["reasoning_effort"],
                         turn_tier=turn.service_tier)

    def _store_turn(self, task_id: str, log: TaskLog, thread_id: str, total: dict, *, kind: str, status: str,
                    started_at: Optional[str], model: Optional[str], effort: str, turn_id: Optional[str] = None,
                    context: Optional[dict] = None, turn_tier: Optional[str] = None, observer=None) -> None:
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
        task = self.get(task_id)
        row = self.db.add_turn(
            **self.ctx.turn_columns(task, turn_tier, turns[-1] if turns else None, started_at, observer),
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
        fields.update(self.ctx.finish_turn(task, row, turns, observer))  # cache health, compactions, tool outputs
        self.db.update_task(task_id, **fields)

    async def _refresh_loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.git_refresh_seconds)
            await self.refresh_git_summary(task_id)

    async def refresh_git_summary(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task and not task["worktree_removed"] and not task["worktree_pending"]:
            self.db.update_task(task_id, git_summary=await git.summary(task["worktree"], task["base_sha"]))

    # ---------- stop ----------

    async def stop(self, task_id: str) -> dict:
        """The user's Stop. The task ends as `stopped`: never retried, and its dependents become blocked."""
        task = self.get(task_id)
        status = task["status"]
        if status in ("waiting_dependencies", "retry_wait", "blocked"):  # nothing is running: just cancel the wait
            try:
                stopped = self.db.set_status(task_id, "stopped", finished_at=now_iso(), next_retry_at=None,
                                             status_detail="stopped by the user")
            except InvalidTransition as e:
                raise TaskError(f"task changed while it was being stopped: {e}", 409) from e
            TaskLog.note(self.log_path(task_id), f"stopped by the user while {status}")
            return stopped
        if status not in ACTIVE_STATUSES:
            raise TaskError(f"task is {status}, not active", 409)
        if status == "queued":
            job = self._jobs.get(task_id)
            if job:
                job.cancel()
            try:
                return self.db.set_status(task_id, "stopped", finished_at=now_iso(), status_detail="stopped by the user")
            except InvalidTransition as e:
                raise TaskError(f"task changed while it was being stopped: {e}", 409) from e
        if task_id not in self._jobs:
            return self._stop_orphan(task)
        self._stop_requested.add(task_id)
        if self.uses_app_server:
            await self._interrupt(task_id)  # no-op until the turn id is known; the turn loop repeats it then
            return task
        proc = self._procs.get(task_id)
        if proc:  # don't block the HTTP request for the SIGTERM -> SIGKILL grace period
            asyncio.create_task(terminate_process(proc, self.settings.stop_grace_seconds))
        return task

    def _stop_orphan(self, task: dict) -> dict:
        """Stop a running task that has no job in this process (it was adopted after a GUI restart, or its job is gone)."""
        task_id = task["id"]
        if same_process(task["pid"], task["proc_identity"]):
            try:
                os.killpg(task["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            asyncio.create_task(self._kill_later(task["pid"], task["proc_identity"]))
        self._adopted.discard(task_id)
        TaskLog.note(self.log_path(task_id), "stopped by the user (the process was not started by this GUI session)")
        self._end_attempt(task_id, "stopped")
        try:
            return self.db.set_status(task_id, "stopped", finished_at=now_iso(), status_detail="stopped by the user")
        except InvalidTransition as e:
            raise TaskError(f"task changed while it was being stopped: {e}", 409) from e

    async def _kill_later(self, pid: int, identity: Optional[str]) -> None:
        await asyncio.sleep(self.settings.stop_grace_seconds)
        if same_process(pid, identity):  # still the same process after the grace period (never a reused pid)
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    async def shutdown(self) -> None:
        """Called when the GUI exits: running turns are stopped. Threads stay on disk and can be resumed later."""
        self._shutting_down = True
        await self.scheduler.stop()
        self.db.remove_status_listener(self._status_changed)
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

    def detach(self) -> None:
        """Stop reacting to database changes (a manager that has been replaced, e.g. by a test simulating a restart)."""
        self.db.remove_status_listener(self._status_changed)

    # ---------- dependencies ----------

    def _status_changed(self, task_id: str, old: str, new: str) -> None:
        """Database listener: a task that became queued is started; one that reached a final state re-evaluates its
        dependents. Both are idempotent atomic steps, so being called twice (or from the scheduler too) does no harm."""
        try:
            if new == "queued":
                self._dispatch(task_id)
            if new in TERMINAL_STATUSES:
                self._evaluate_dependents(task_id)
        finally:
            self.scheduler.wake()

    def _evaluate_one(self, task_id: str) -> str:
        """Move one waiting_dependencies task on if its prerequisites say so: to queued when all are completed (policy
        all_success), to blocked when one failed, was stopped or is blocked. "waiting" = nothing to do yet."""
        task = self.db.get_task(task_id)
        if task is None or task["status"] != "waiting_dependencies":
            return ""
        if self.db.queue_if_ready(task_id):  # the check and the change are one UPDATE; the listener starts the task
            TaskLog.note(self.log_path(task_id), "all dependencies completed; the task is queued")
            return "queued"
        failed = [(self.db.get_task(d) or {}) for d in self.db.dependencies_of(task_id)]
        failed = [d for d in failed if d.get("status") in DEPENDENCY_FAILED_STATUSES]
        if failed:
            detail = "; ".join(f"Dependency {d['name']} {DEPENDENCY_PHRASES[d['status']]}" for d in failed)
            if self.db.block_if_failed(task_id, detail):
                TaskLog.note(self.log_path(task_id), f"blocked: {detail}")
                return "blocked"
        issues = [self.db.get_task(d) for d in self.db.dependencies_of(task_id)]
        detail = "; ".join(f"{d['name']} {d['task_outcome'].replace('_', ' ')}: {d['outcome_reason']}"
                           for d in issues if d and d["status"] == "completed" and d["task_outcome"] != "success")
        self.db.update_task(task_id, status_detail=detail[:1000])
        return "waiting"

    def _evaluate_dependents(self, task_id: str) -> None:
        for child in self.db.dependents_of(task_id):
            self._evaluate_one(child)

    async def set_dependencies(self, task_id: str, depends_on) -> dict:
        """Replace the prerequisites of a task that has not started (waiting_dependencies or blocked)."""
        task = self.get(task_id)
        if task["status"] not in ("waiting_dependencies", "blocked"):
            raise TaskError(f"task is {task['status']}; dependencies can only be changed before it starts", 409)
        ids = self._check_dependency_ids(depends_on)
        try:
            self.db.replace_dependencies(task_id, ids)
        except DependencyError as e:
            raise TaskError(str(e), 400, e.code) from e
        if task["status"] == "blocked":
            self.db.transition(task_id, "blocked", "waiting_dependencies", status_detail="", finished_at=None)
        self._evaluate_one(task_id)
        return self.get(task_id)

    async def run_anyway(self, task_id: str) -> dict:
        """Start a waiting or blocked task without waiting for (or caring about) its prerequisites. Only on the user's click."""
        task = self.get(task_id)
        if task["status"] not in ("waiting_dependencies", "blocked"):
            raise TaskError(f"task is {task['status']}, not waiting for dependencies", 409)
        ok = self.db.transition(task_id, task["status"], "queued", claimed_by=None, claimed_at=None, status_detail="",
                                finished_at=None, pending_turn=self._run_anyway_turn(task).to_json())
        if not ok:
            raise TaskError("task changed meanwhile; try again", 409)
        TaskLog.note(self.log_path(task_id), "Run Anyway: started without waiting for all dependencies")
        self._dispatch(task_id)
        return self.get(task_id)

    def _run_anyway_turn(self, task: dict) -> _Turn:
        turn = self._pending_turn(task)
        turn.ignore_dependencies = True
        return turn

    async def retry_failed_dependencies(self, task_id: str, confirm_over_limit: bool = False) -> dict:
        """For a blocked task: retry the prerequisites that failed or were stopped (each in its own thread and worktree) and
        let the task wait for them again. A prerequisite that is itself blocked is fixed the same way, up the chain."""
        task = self.get(task_id)
        if task["status"] != "blocked":
            raise TaskError(f"task is {task['status']}, not blocked", 409)
        retried, problems = [], []
        self._retry_dependencies(task_id, confirm_over_limit, retried, problems, set())
        if problems and not retried:
            raise TaskError("; ".join(problems), 409)
        if self.db.get_task(task_id)["status"] == "blocked":
            self.db.transition(task_id, "blocked", "waiting_dependencies", status_detail="", finished_at=None)
        self._evaluate_one(task_id)
        return {**self.get(task_id), "retried": retried, "problems": problems}

    def _retry_dependencies(self, task_id: str, confirm: bool, retried: list, problems: list, seen: set) -> None:
        for dep_id in self.db.dependencies_of(task_id):
            dep = self.db.get_task(dep_id)
            if dep is None or dep_id in seen:
                continue
            seen.add(dep_id)
            try:
                if dep["status"] in ("failed", "stopped"):
                    self._retry(dep, "manual_retry", confirm)
                    retried.append(dep_id)
                elif dep["status"] == "blocked":
                    self._retry_dependencies(dep_id, confirm, retried, problems, seen)
                    if self.db.transition(dep_id, "blocked", "waiting_dependencies", status_detail="", finished_at=None):
                        retried.append(dep_id)
                    self._evaluate_one(dep_id)
            except TaskError as e:
                problems.append(f"{dep['name']}: {e}")

    # ---------- retry ----------

    async def retry_task(self, task_id: str, confirm_over_limit: bool = False) -> dict:
        """[Retry] / [Retry Now]: run a failed or stopped task again, or end the wait of a task in retry_wait. Same worktree and
        same Codex thread (use Start New Session for another one). Past the automatic limit it needs `confirm_over_limit`."""
        task = self.get(task_id)
        if task["status"] == "retry_wait":
            if not self._start_retry(task_id, "manual_retry"):
                raise TaskError("task changed meanwhile; try again", 409)
            return self.get(task_id)
        if task["status"] not in ("failed", "stopped"):
            raise TaskError(f"task is {task['status']}; only a failed or stopped task can be retried", 409)
        return self._retry(task, "manual_retry", confirm_over_limit)

    def _retry(self, task: dict, trigger: str, confirm_over_limit: bool) -> dict:
        if not task["worktree_pending"]:
            self._require_worktree(task)  # a deleted worktree is never recreated
        if task["retry_count"] > 0 and task["retry_count"] >= task["max_retries"] and not confirm_over_limit:
            raise TaskError(f"the automatic retry limit was reached ({task['retry_count']}/{task['max_retries']}); "
                            "retrying again needs confirmation", 409, "over_limit")
        turn = self._turn_of(task, trigger)
        try:
            self.db.set_status(task["id"], "queued", pid=None, proc_identity=None, exit_code=None, finished_at=None,
                               status_detail="", pending_turn=turn.to_json(), claimed_by=None, claimed_at=None,
                               next_retry_at=None, last_retry_at=timestamp())
        except InvalidTransition as e:
            raise TaskError(f"task changed meanwhile: {e}", 409) from e
        self._dispatch(task["id"])
        return self.get(task["id"])

    def _start_retry(self, task_id: str, trigger: Optional[str] = None) -> bool:
        """retry_wait -> queued, atomically (a due timer and a click on Retry Now cannot both do it), then started."""
        task = self.db.get_task(task_id)
        if task is None or task["status"] != "retry_wait":
            return False
        turn = self._turn_of(task, trigger)
        ok = self.db.transition(task_id, "retry_wait", "queued", pending_turn=turn.to_json(), claimed_by=None, claimed_at=None,
                                next_retry_at=None, last_retry_at=timestamp(), pid=None, proc_identity=None, exit_code=None,
                                finished_at=None, status_detail="")
        if ok:
            self._dispatch(task_id)
        return ok

    async def set_auto_retry(self, task_id: str, enabled: Optional[bool] = None, max_retries: Optional[int] = None) -> dict:
        task = self.get(task_id)
        fields = {}
        if enabled is not None:
            fields["auto_retry_enabled"] = int(enabled)
        if max_retries is not None:
            fields["max_retries"] = self._check_max_retries(max_retries)
        if task["status"] == "retry_wait" and enabled is False:  # cancel the pending retry; Retry still works by hand
            ok = self.db.transition(task_id, "retry_wait", "failed", finished_at=now_iso(), next_retry_at=None,
                                    status_detail=f"automatic retry turned off: {task['last_failure_message']}"[:500], **fields)
            if ok:
                TaskLog.note(self.log_path(task_id), "automatic retry turned off; the pending retry was cancelled")
                return self.get(task_id)
        return self.db.update_task(task_id, **fields) if fields else task

    # ---------- the scheduler's tick ----------

    def tick(self) -> None:
        """One scheduling pass (the Scheduler calls it every few seconds and whenever a status changes). Idempotent."""
        if self._shutting_down:
            return
        for name, step in (("dependencies", self._tick_dependencies), ("retries", self._tick_retries),
                           ("orphans", self._tick_orphans), ("scheduled", self._tick_scheduled), ("queue", self._tick_queue)):
            try:
                step()
            except Exception:
                logger.exception("scheduler step %s failed", name)

    # ---------- scheduled instructions (a follow-up turn for an existing thread, after other tasks) ----------

    async def schedule_instruction(self, task_id: str, prompt: str, depends_on=(), service_tier: Optional[str] = None,
                                   created_by: str = "user", attachment_ids=()) -> dict:
        """Reserve an instruction for the Codex thread of `task_id`. It is sent when every task in `depends_on` has
        completed AND the thread is idle (no `depends_on` = as soon as the thread is idle), on the SAME thread, worktree and
        branch. Its speed is its own: `service_tier` None means Standard, never "whatever the last turn used".
        Reasoning effort and approval come from the task."""
        prompt = prompt.strip()
        if not prompt and not attachment_ids:
            raise TaskError("instruction is required", 400, "empty")
        await self._check_images(attachment_ids, resume=True)
        task = self.get(task_id)
        tier = self._turn_tier(service_tier) or "default"
        deps = self._check_dependency_ids(depends_on)
        if task_id in deps:
            raise TaskError("an instruction cannot depend on its own target task: it waits for the thread to be idle anyway", 400, "self")
        if task["worktree_removed"]:
            raise TaskError("worktree no longer exists", 409, "no_worktree")
        try:
            row = self.db.create_scheduled(task_id, prompt, tier, deps, created_by, attachment_ids)
        except ScheduledError as e:
            raise TaskError(str(e), 400, e.code) from e
        TaskLog.note(self.log_path(task_id), f"scheduled instruction #{row['id']} created ({'Fast' if tier == 'priority' else tier if tier != 'default' else 'Standard'}"
                     + (f"; after {len(deps)} task(s)" if deps else "; when the thread is idle") + ")")
        self._tick_scheduled()  # it may be sendable right now
        self.scheduler.wake()
        return self._scheduled_view(self.db.get_scheduled(row["id"]))

    def cancel_scheduled(self, task_id: str, sid: int) -> dict:
        """Cancel an instruction that has not been sent. A cancelled instruction is never sent; one that is running is stopped
        with the task's own Stop."""
        self.get(task_id)
        row = self.db.get_scheduled(sid)
        if row is None or row["task_id"] != task_id:
            raise TaskError("scheduled instruction not found", 404)
        if not self.db.cancel_scheduled(sid):
            now = self.db.get_scheduled(sid)["status"]
            raise TaskError(f"the instruction is {now}; " + ("stop the task to stop it" if now == "running" else "it can no longer be cancelled"),
                            409, "not_cancellable")
        TaskLog.note(self.log_path(task_id), f"scheduled instruction #{sid} cancelled")
        self.scheduler.wake()  # the next instruction of the thread may be at the head of the queue now
        return self._scheduled_view(self.db.get_scheduled(sid))

    def scheduled_instructions(self, task_id: str) -> list[dict]:
        self.get(task_id)
        return [self._scheduled_view(r) for r in self.db.list_scheduled(task_id)]

    def _scheduled_view(self, row: dict) -> dict:
        deps = []
        for dep_id in self.db.scheduled_dependencies(row["id"]):
            dep = self.db.get_task(dep_id)
            if dep:
                deps.append({"id": dep_id, "name": dep["name"], "status": dep["status"],
                             "task_outcome": dep["task_outcome"], "outcome_reason": dep["outcome_reason"]})
        out = {k: row[k] for k in ("id", "task_id", "prompt", "status", "service_tier", "created_at", "ready_at", "started_at",
                                   "finished_at", "blocked_reason", "created_by")}
        out["attachments"] = self.attachments.views(json.loads(row["attachment_ids"]))
        out.update(speed="Fast" if row["service_tier"] == "priority" else "Standard" if row["service_tier"] == "default" else row["service_tier"],
                   dependencies=deps, deps_done=sum(d["status"] == "completed" and d["task_outcome"] == "success" for d in deps), deps_total=len(deps))
        if row["status"] == "waiting_thread":
            target = self.db.get_task(row["task_id"]) or {}
            out["wait_note"] = f"the thread's task is {target.get('status', '?')}"
        return out

    def _scheduled_dead_end(self, row: dict) -> str:
        """Why this instruction can never be sent however long it waits ("" = it can). Only facts that do not heal by themselves."""
        task = self.db.get_task(row["task_id"])
        if task is None:
            return "the target task no longer exists"
        if task["worktree_removed"]:
            return "the target task's worktree was deleted"
        if task["status"] == "completed" and not task["codex_thread_id"]:
            return "the target task has no recorded Codex thread to continue"
        return ""

    def _tick_scheduled(self) -> None:
        """One pass over the scheduled instructions. Idempotent and safe to run concurrently with itself: every step is a
        conditional UPDATE (see Database.advance_scheduled / claim_scheduled), so the worst a duplicate pass can do is nothing.

        1. running instructions whose turn has ended are finished (completed, or failed when the task ended failed / stopped);
        2. waiting ones are moved to the state their dependencies and their thread justify (blocked / waiting_dependencies /
           waiting_thread / ready);
        3. the oldest ready instruction of every thread that has nothing running is claimed and its turn queued.
        """
        if self._shutting_down:
            return
        for row in self.db.list_scheduled(statuses=["running"]):
            self._reconcile_scheduled(row)
        self._advance_all_scheduled()
        claimed = False
        for row in self.db.ready_scheduled():
            claimed = self._claim_scheduled(row) or claimed
        if claimed:
            self._advance_all_scheduled()  # the others of that thread wait for it now (ready -> waiting_thread)

    def _advance_all_scheduled(self) -> None:
        for row in self.db.list_scheduled(statuses=SCHEDULED_WAITING):
            sid = row["id"]
            dead = self._scheduled_dead_end(row)
            if dead:
                self.db.block_scheduled(sid, dead)
                continue
            failed = self.db.scheduled_dependency_failures(sid)
            reason = "; ".join(f"Dependency {d['name']} {DEPENDENCY_PHRASES[d['status']]}" for d in failed)
            if self.db.advance_scheduled(sid, reason) == "blocked":
                TaskLog.note(self.log_path(row["task_id"]), f"scheduled instruction #{sid} blocked: {reason}")

    def _claim_scheduled(self, row: dict) -> bool:
        """Send one ready instruction: its turn becomes the task's pending turn on the task's own Codex thread. The claim is
        a single transaction (Database.claim_scheduled); only the caller that wins it logs and starts anything."""
        lock = self._completion_locks.get(row["task_id"])
        if lock and lock.locked():
            return False
        task = self.db.get_task(row["task_id"])
        thread = task and task["codex_thread_id"]
        if not task or not thread or task["status"] != "completed":
            return False
        turn = _Turn(row["prompt"], resume_thread=thread, trigger="scheduled_instruction", service_tier=row["service_tier"],
                     attachment_ids=json.loads(row["attachment_ids"]))
        if not self.db.claim_scheduled(row["id"], self._queue_fields(turn), thread):
            return False
        TaskLog.note(self.log_path(task["id"]), f"scheduled instruction #{row['id']} sent to Codex thread {thread} "
                     f"({'Fast' if row['service_tier'] == 'priority' else 'Standard' if row['service_tier'] == 'default' else row['service_tier']})")
        return True

    def _reconcile_scheduled(self, row: dict) -> None:
        """A running instruction ends with the turn it started. An unexpected stop of that turn is the task's own recovery
        (retry_wait -> the same thread and worktree): the instruction stays running and is never sent again. A paused task
        (interrupted, waiting-for-quota) keeps it running too: resuming it continues this same turn."""
        task = self.db.get_task(row["task_id"])
        if task is None:
            self.db.finish_scheduled(row["id"], "failed", "the target task no longer exists")
        elif task["status"] == "completed":
            self.db.finish_scheduled(row["id"], "completed" if task["task_outcome"] == "success" else "failed",
                                    "" if task["task_outcome"] == "success" else task["outcome_reason"])
        elif task["status"] in ("failed", "stopped"):
            detail = task["status_detail"] or ("stopped by the user" if task["status"] == "stopped" else "")
            self.db.finish_scheduled(row["id"], "failed", f"the task {task['status']}" + (f": {detail}" if detail else ""))

    def _tick_dependencies(self) -> None:
        for task in self.db.list_tasks_by_status(["waiting_dependencies"]):
            self._evaluate_one(task["id"])

    def _tick_retries(self) -> None:
        for task_id in self.db.due_retries(timestamp()):
            self._start_retry(task_id)

    def _tick_queue(self) -> None:
        for task in self.db.list_tasks_by_status(["queued"]):
            if not task["claimed_by"]:
                self._dispatch(task["id"])

    def _tick_orphans(self) -> None:
        """Running tasks that no job of this process is running: adopted ones are watched until their process ends, any
        other is reconciled (this also covers a job that vanished without settling its task)."""
        for task in self.db.list_tasks_by_status(["starting", "running"]):
            task_id = task["id"]
            if task_id in self._jobs:
                continue
            if task_id in self._adopted:
                if not same_process(task["pid"], task["proc_identity"]):
                    self._adopted.discard(task_id)
                    self._process_lost(task, "the process that was running this task is gone")
            else:
                self._reconcile(task)

    # ---------- restart recovery ----------

    def recover(self) -> list[str]:
        """Called once when the GUI starts: look again at every task the previous GUI process left in the middle of something.

        - running / starting: if the recorded process is still alive (same pid AND same start time AND still a codex command
          line, so a reused pid does not count) the task is left alone and watched; if it is gone the task is retried
          (retry_wait) when automatic retry is on and not used up, else failed. Nothing is ever started twice.
        - queued: never started by the old process; released so the scheduler starts it.
        - retry_wait: its timer lives in the database; only the worktree is checked again.
        Assumes one GUI process per database (it takes over claims of a previous process).
        """
        fixed = []
        for task in self.db.list_tasks_by_status(["queued", "starting", "running", "retry_wait", "completed"]):
            if self._reconcile(task, startup=True):
                fixed.append(task["id"])
        return fixed

    def _reconcile(self, task: dict, startup: bool = False) -> bool:
        task_id, status = task["id"], task["status"]
        if task_id in self._jobs:
            return False
        if task["completion_pending"] and status in ("running", "completed"):
            # Codex finished normally; the GUI disappeared while checking. A process retry would duplicate work.
            fields = dict(completion_pending=0, task_outcome="needs_review", outcome_source="gate",
                          outcome_reason="GUI restarted during completion checks; re-run completion checks.",
                          completion_checked_at=None, next_retry_at=None, pending_turn=None, finished_at=now_iso(),
                          evidence_result="UNKNOWN", evidence_reason="Completion checks were interrupted.",
                          approval_source="", approved_at=None)
            if status == "completed":
                self.db.update_task(task_id, **fields)
            else:
                self.db.set_status(task_id, "completed", **fields)
            attempt = self.db.open_attempt(task_id)
            if attempt:
                self.db.update_attempt(attempt["id"], result="completed", finished_at=now_iso(), exit_code=task["exit_code"])
            return True
        if status == "completed":
            return False
        if status == "queued":
            if not task["pending_turn"]:  # a row from before pending_turn existed: what it was to run is unknown, so the user decides
                TaskLog.note(self.log_path(task_id), "GUI restarted while this task was queued; marking it interrupted.")
                self.db.set_status(task_id, "interrupted", finished_at=now_iso())
                return True
            if not task["claimed_by"]:
                return False
            TaskLog.note(self.log_path(task_id), "GUI restarted while this task was queued; it will be started again.")
            self.db.release_claim(task_id)
            return True
        if status == "retry_wait":
            if task["worktree_removed"] or not os.path.isdir(task["worktree"]):
                TaskLog.note(self.log_path(task_id), "GUI restarted during the retry wait and the worktree no longer exists; not retrying.")
                self.db.set_status(task_id, "failed", finished_at=now_iso(), next_retry_at=None,
                                   status_detail="the worktree no longer exists; it is not recreated automatically")
                return True
            return False
        # starting / running without a job in this process
        if same_process(task["pid"], task["proc_identity"]):
            self._adopted.add(task_id)
            note = (f"GUI restarted while this task was running; process {task['pid']} is still alive and is left running. "
                    "Its output can no longer be captured; the task is retried if that process ends unexpectedly.")
            TaskLog.note(self.log_path(task_id), note)
            self.db.update_task(task_id, status_detail="running without output capture (GUI restarted)")
            return True
        self._process_lost(task, "GUI restarted while this task was running and its Codex process is gone" if startup
                           else "the process that was running this task is gone")
        return True

    def _process_lost(self, task: dict, why: str) -> None:
        """A task whose process disappeared: retried (retry_wait) within its limit, else failed. Same path as any unexpected stop."""
        log = TaskLog(self.log_path(task["id"]))
        try:
            log.add_system(why)
            self._settle(task["id"], log, self._in_flight_turn(task), "failed",
                         failure=recovery.process_lost(why), retry_trigger="restart_recovery")
        finally:
            log.close()

    # ---------- git views and operations ----------

    def _require_worktree(self, task: dict) -> str:
        if task["worktree_pending"]:
            raise TaskError("the worktree has not been created yet; it is created when the task starts", 409, "no_worktree")
        if task["worktree_removed"] or not os.path.isdir(task["worktree"]):
            raise TaskError("worktree no longer exists", 409, "no_worktree")
        return task["worktree"]

    def _require_inactive(self, task: dict) -> None:
        self._require_completion_idle(task["id"])
        if task["status"] in BUSY_STATUSES:
            raise TaskError("task is still active or waiting; stop it first", 409)

    async def git_info(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task["worktree_pending"]:
            return {"available": False, "branch": task["branch"], "note": "worktree not created yet (it is created when the task starts)"}
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
        # Context Efficiency: what was requested for the turn and what it did (None for turns recorded before these existed)
        for k in ("service_tier", "verbosity", "tool_profile", "requests", "max_request_input", "tool_calls", "large_tool_outputs",
                  "tool_output_tokens_est", "compactions", "idle_before_seconds"):
            out[k] = row.get(k)
        return out

    def _present(self, task: dict, latest: Optional[dict], by_id: Optional[dict] = None,
                 edges: Optional[dict] = None, scheduled: Optional[dict] = None) -> dict:
        """The task plus the derived figures the UI shows (cache, context, quota, model, retry suggestion, dependencies)."""
        for internal in ("pending_turn", "claimed_by", "claimed_at", "proc_identity"):
            task.pop(internal, None)
        task["execution_status"] = task["status"]
        task.update(self._dependency_view(task, by_id, edges))
        task["retry_in_seconds"] = self._seconds_until(task["next_retry_at"]) if task["status"] == "retry_wait" else None
        task["cache_hit_rate"] = self.present_turn(latest)["cache_hit_rate"] if latest and latest["kind"] == "turn" else None
        task["effective_model"] = (latest or {}).get("model") or task["model"] or ""
        task["context"] = context_status(task["context_tokens"], task["context_window"],
                                         self.settings.context_warn_percent, bool(task["context_guard"]))
        task["backend"] = self.settings.backend
        sched = (scheduled or {}).get(task["id"], {})
        task["scheduled_pending"], task["scheduled_ready"] = sched.get("pending", 0), sched.get("ready", 0)
        zone = ctx_guard.context_zone(task["context_tokens"], task["effective_model"] or task["model"], task["context_window"])
        task["ctx_zone"] = zone["zone"]
        return task

    @staticmethod
    def _seconds_until(iso: Optional[str]) -> Optional[float]:
        try:
            return max(0.0, (datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now().astimezone()).total_seconds())
        except (AttributeError, ValueError):
            return None

    def _dependency_view(self, task: dict, by_id: Optional[dict], edges: Optional[dict]) -> dict:
        """Prerequisites with their current status, and how many are completed ("Waiting (2/3 complete)")."""
        ids = edges.get(task["id"], []) if edges is not None else self.db.dependencies_of(task["id"])
        deps = []
        for dep_id in ids:
            dep = by_id.get(dep_id) if by_id is not None else self.db.get_task(dep_id)
            if dep:
                deps.append({"id": dep_id, "name": dep["name"], "status": dep["status"],
                             "task_outcome": dep["task_outcome"], "outcome_reason": dep["outcome_reason"]})
        return {"dependencies": deps, "deps_total": len(deps),
                "deps_done": sum(d["status"] == "completed" and d["task_outcome"] == "success" for d in deps)}

    def present_task(self, task: dict) -> dict:
        turns = self.db.list_turns(task["id"])
        latest = next((t for t in reversed(turns) if t["kind"] == "turn"), turns[-1] if turns else None)
        task = self._present(dict(task), latest, scheduled=self.db.scheduled_counts())
        task["messages"] = [row | {"attachments": self.attachments.views(row["attachment_ids"])}
                            for row in self.db.list_messages(task["id"])]
        task["scheduled_instructions"] = [self._scheduled_view(r) for r in self.db.list_scheduled(task["id"])]
        task["dependents"] = [{"id": d["id"], "name": d["name"], "status": d["status"]}
                              for d in map(self.db.get_task, self.db.dependents_of(task["id"])) if d]
        task["observed_quota"] = self._observed_quota(task)
        task["retry_suggestion"] = self._retry_suggestion(task)
        task["completion_contract"] = json.loads(task["completion_contract"] or "{}")
        task["completion_checks"] = json.loads(task["completion_checks"] or "[]")
        task["completion_overrides"] = self.db.completion_overrides(task["id"])
        task["completion_approvals"] = self.db.completion_approvals(task["id"])
        result = completion.parse_result(task["semantic_result"])
        task["semantic_status"] = result["status"] if result else "UNKNOWN"
        task["ctx"] = self.ctx.task_view(task, turns, task["effective_model"] or task["model"],
                                         self.ctx.peek_model(task["effective_model"] or task["model"]).get("tool_output_cap"))
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
        tasks = self.db.list_tasks()
        by_id, edges, scheduled = {t["id"]: t for t in tasks}, self.db.dependency_map(), self.db.scheduled_counts()
        return [self._present(t, latest.get(t["id"]), by_id, edges, scheduled) for t in tasks]

    def attempts(self, task_id: str) -> list[dict]:
        self.get(task_id)
        return self.db.list_attempts(task_id)

    def usage(self, task_id: str) -> dict:
        self.get(task_id)
        turns = [self.present_turn(r) for r in self.db.list_turns(task_id)]
        latest = next((t for t in reversed(turns) if t["kind"] == "turn"), None)
        task = self.db.get_task(task_id)
        rows = [(t, task["model"], task["service_tier"], task["auto_approval"]) for t in self.db.list_turns(task_id)]
        return {"turns": turns, "latest": latest, "efficiency": efficiency.aggregate(rows)}

    # ---------- context efficiency API ----------

    async def context_preview(self, repository: str, cwd_subdir: str = "", tool_profile: str = "full", tool_output: str = "default",
                              skills: str = "default", skills_budget: Optional[int] = None, allow_subagents: bool = False) -> dict:
        """What the model will start with for a repository (New Task form): the AGENTS.md chain with the effective
        project_doc_max_bytes, the skills catalog under the chosen budget, MCP servers, the model's tool-output cap."""
        repo_input = Path(repository.strip()).expanduser()
        repo = await git.repo_toplevel(repo_input) if repository.strip() and repo_input.is_dir() else None
        if repo is None:
            raise TaskError(f"not a git repository: {repository}")
        sub = await check_subdir_in_ref(repo, "HEAD", cwd_subdir) if cwd_subdir.strip() else ""
        cwd = str(Path(repo) / sub) if sub else repo
        cfg = await self.effective_config(cwd)
        try:
            _, budget = ctx_config.resolve_skills(skills, skills_budget)
        except ValueError as e:
            raise TaskError(str(e)) from e
        out = await self.ctx.preview(cwd, cfg, {"skills.max_context_tokens": budget} if budget else None)
        default_skills = await self.ctx.preview_skills(cwd) if budget else out["skills"]
        out.update(cwd=cwd, repository=repo, config_available=cfg is not None, default_skills=default_skills,
                   edit_path=f"/agents?repository={repo}")
        return out

    async def verify_tool_profile(self, repository: str, profile: str, force: bool = False) -> dict:
        repo = await git.repo_toplevel(Path(repository.strip()).expanduser()) if repository.strip() else None
        if repo is None:
            raise TaskError(f"not a git repository: {repository}")
        if profile not in ctx_config.TOOL_PROFILES:
            raise TaskError(f"unknown tool profile: {profile}")
        mcp = ctx_config.mcp_server_names(await self.effective_config(repo)) if profile == "minimal" else []
        return await self.ctx.verify_profile(repo, profile, mcp, force=force)

    async def change_tool_profile(self, task_id: str, profile: str, confirm: bool = False) -> dict:
        """The one way to change a task's tool profile after it started. It rewrites the model's tool definitions, so the
        prompt cache of the thread may be lost: refused unless the user confirms that warning."""
        task = self.get(task_id)
        self._require_idle_with_worktree(task)
        if profile not in ctx_config.TOOL_PROFILES:
            raise TaskError(f"unknown tool profile: {profile}")
        if profile == task["tool_profile"]:
            raise TaskError("the task already uses this tool profile", 409, "unchanged")
        if not confirm:
            raise TaskError("Changing tool configuration may reduce prompt cache reuse", 409, "confirm_cache_loss")
        mcp = ctx_config.mcp_server_names(await self.effective_config(task["repository"])) if profile == "minimal" else []
        cfg = ctx_config.profile_config(profile, mcp)
        updated = self.db.update_task(task_id, tool_profile=profile, tool_profile_config=json.dumps(cfg, sort_keys=True) if cfg else "",
                                      tool_profile_check="")
        self.db.add_context_event(task_id, "tool_profile_change", "warning",
                                  f"Tool profile changed {task['tool_profile']} -> {profile} (requested by the user): "
                                  "tool definitions changed, so the prompt cache may not be reused", now_iso(), None,
                                  {"from": task["tool_profile"], "to": profile})
        TaskLog.note(self.log_path(task_id), f"tool profile changed {task['tool_profile']} -> {profile} (requested by the user)")
        if profile != "full":
            self._verify_in_background(task_id)
        return updated

    def acknowledge_long_context(self, task_id: str) -> dict:
        """The user's [Continue] in the long-context banner: remember the zone so the banner stops asking. Nothing else happens."""
        task = self.get(task_id)
        turns = self.db.list_turns(task_id)
        zone = ctx_guard.context_zone(task["context_tokens"], (turns[-1]["model"] if turns else None) or task["model"] or None,
                                      task["context_window"])
        if zone["zone"] not in ("warning", "strong", "long"):
            raise TaskError("the context is not in a warning zone", 409, "no_zone")
        return self.db.update_task(task_id, long_context_ack=zone["zone"])

    def efficiency_for(self, period: str = "lifetime") -> dict:
        """Efficiency of all tasks over a period (today / 7d / lifetime), by the turns' own timestamps."""
        turns = self.db.list_all_turns(efficiency.period_start(period))
        return {**efficiency.aggregate((t, t["task_model"], t["task_tier"], t["task_auto"]) for t in turns),
                "period": period}

    def counts(self, tasks: list[dict]) -> dict:
        counts = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return counts

    @staticmethod
    def is_terminal(task: dict) -> bool:
        return task["status"] in TERMINAL_STATUSES
