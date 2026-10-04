"""SQLite persistence for tasks and recently used repositories."""
import logging
import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from .models import (
    DEPENDENCY_FAILED_STATUSES, InvalidTransition, SCHEDULED_CANCELLABLE, SCHEDULED_IDLE_TASK_STATUS, SCHEDULED_WAITING,
    STATUSES, can_transition, now_iso,
)

log = logging.getLogger(__name__)


class DependencyError(ValueError):
    """A dependency edge that must not exist. `code`: "self" | "duplicate" | "cycle" | "missing"."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code

class ScheduledError(ValueError):
    """A scheduled instruction that must not exist. `code`: "empty" | "self" | "duplicate" | "missing" | "no_task"."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class _Rollback(Exception):
    """Raised inside a transaction to undo it (the conditional UPDATE that had to go with another one did not match)."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repository TEXT NOT NULL,
    worktree TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    reasoning_effort TEXT NOT NULL DEFAULT 'default',
    auto_approval INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    pid INTEGER,
    exit_code INTEGER,
    git_summary TEXT NOT NULL DEFAULT '',
    worktree_removed INTEGER NOT NULL DEFAULT 0,
    branch_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    codex_thread_id TEXT,
    last_turn_at TEXT
);
-- One row per completed Codex turn. The *_tokens columns are that turn's own usage;
-- total_json is the thread total exactly as the CLI reported it (the baseline for the next turn).
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    session INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_write_input_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_json TEXT NOT NULL,
    UNIQUE (task_id, turn)
);
-- Rate-limit snapshots (from account/rateLimits/read and its push updates), so that usage can be analysed later.
-- Display and analysis only: nothing reads this table to steer the number of tasks.
CREATE TABLE IF NOT EXISTS rate_limit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    task_id TEXT,
    plan_type TEXT,
    limit_id TEXT,
    primary_used_percent REAL,
    primary_window_mins INTEGER,
    primary_resets_at INTEGER,
    secondary_used_percent REAL,
    secondary_window_mins INTEGER,
    secondary_resets_at INTEGER,
    ordinary_usage_allowed INTEGER,
    reached_type TEXT,
    available_resets INTEGER
);
-- Many-to-many prerequisites: task_id starts only after every depends_on_task_id has completed (policy all_success).
-- The edges form a DAG: add_dependencies() refuses self edges, duplicates and anything that would close a cycle.
CREATE TABLE IF NOT EXISTS task_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_parent ON task_dependencies (depends_on_task_id);
-- A follow-up turn for the EXISTING Codex thread of `task_id`, sent when every depends_on task has completed AND the
-- thread is idle (see models.SCHEDULED_*). Not a task dependency: the task itself is never made to wait for these.
-- Zero dependency rows = "send when the thread is idle". service_tier is explicit ("default" = Standard, "priority" = Fast).
-- blocked_reason explains blocked / failed / cancelled rows. Order of sending per task: created_at, then id (FIFO).
CREATE TABLE IF NOT EXISTS scheduled_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    service_tier TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL,
    ready_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    blocked_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS idx_scheduled_instructions_task ON scheduled_instructions (task_id, status);
CREATE TABLE IF NOT EXISTS scheduled_instruction_dependencies (
    scheduled_instruction_id INTEGER NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    UNIQUE (scheduled_instruction_id, depends_on_task_id)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_deps_task ON scheduled_instruction_dependencies (depends_on_task_id);
-- One row per run of a Codex turn (the first run, an instruction, an automatic or manual retry, a restart recovery).
-- Kept apart from `turns`, which is token usage: an interrupted attempt may have no usage at all.
CREATE TABLE IF NOT EXISTS task_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    trigger_kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    result TEXT NOT NULL DEFAULT 'running',
    failure_kind TEXT NOT NULL DEFAULT '',
    failure_message TEXT NOT NULL DEFAULT '',
    codex_thread_id TEXT,
    was_resume INTEGER NOT NULL DEFAULT 0,
    service_tier TEXT,
    reasoning_effort TEXT,
    git_head TEXT,
    git_status TEXT,
    unpushed INTEGER,
    UNIQUE (task_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_task_attempts_task ON task_attempts (task_id);
-- Context-efficiency events of a task: cache misses, large tool outputs, context jumps, compactions, retry-guard stops.
CREATE TABLE IF NOT EXISTS context_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    turn INTEGER,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS context_events_task ON context_events (task_id, id);
-- GUI-wide settings (the configurable thresholds of Cache Health / Tool Output Guard).
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recent_repos (
    path TEXT PRIMARY KEY,
    last_used TEXT NOT NULL
);
"""

TASK_COLUMNS = (
    "id name repository worktree branch base_ref base_sha prompt model reasoning_effort "
    "auto_approval status pid exit_code git_summary worktree_removed branch_deleted "
    "created_at started_at finished_at codex_thread_id last_turn_at "
    "service_tier model_verbosity web_search_enabled web_search_mode sandbox network_access adaptive_reasoning context_guard writable_dirs feature_flags "
    "latest_input_tokens latest_cached_input_tokens latest_output_tokens context_tokens context_window "
    "five_hour_used_before five_hour_used_after weekly_used_before weekly_used_after quota_overlap "
    "last_prompt status_detail failure_source "
    "dependency_policy auto_retry_enabled max_retries retry_count next_retry_at last_failure_kind last_failure_message "
    "last_exit_code last_retry_at pending_turn claimed_by claimed_at worktree_pending proc_identity "
    "allow_subagents tool_output_preset tool_output_limit skills_preset skills_budget tool_profile tool_profile_config "
    "tool_profile_check cwd_subdir stop_reason compactions last_cache_activity_at last_request_input long_context_ack"
).split()

# Columns added after the first release: (name, definition) applied to databases that lack them.
# service_tier holds Codex's own id: "default" is Standard speed, "priority" is Fast.
TASK_MIGRATIONS = [
    ("codex_thread_id", "TEXT"), ("last_turn_at", "TEXT"),
    ("service_tier", "TEXT NOT NULL DEFAULT 'default'"),
    ("model_verbosity", "TEXT NOT NULL DEFAULT 'low'"),
    ("web_search_enabled", "INTEGER NOT NULL DEFAULT 0"),
    # "cached" | "live" | "disabled". Empty on tasks that predate it: web_search_enabled then decides (live / disabled).
    ("web_search_mode", "TEXT NOT NULL DEFAULT ''"),
    ("sandbox", "TEXT NOT NULL DEFAULT 'workspace-write'"),
    # Network inside the workspace-write sandbox. Tasks that predate it keep what they had: no network.
    ("network_access", "INTEGER NOT NULL DEFAULT 0"),
    ("adaptive_reasoning", "INTEGER NOT NULL DEFAULT 1"),
    ("context_guard", "INTEGER NOT NULL DEFAULT 1"),
    ("writable_dirs", "TEXT NOT NULL DEFAULT ''"),
    ("feature_flags", "TEXT NOT NULL DEFAULT ''"),
    ("latest_input_tokens", "INTEGER"), ("latest_cached_input_tokens", "INTEGER"), ("latest_output_tokens", "INTEGER"),
    ("context_tokens", "INTEGER"), ("context_window", "INTEGER"),
    ("five_hour_used_before", "REAL"), ("five_hour_used_after", "REAL"),
    ("weekly_used_before", "REAL"), ("weekly_used_after", "REAL"),
    ("quota_overlap", "INTEGER NOT NULL DEFAULT 0"),
    ("last_prompt", "TEXT NOT NULL DEFAULT ''"),
    ("status_detail", "TEXT NOT NULL DEFAULT ''"),
    # "codex": the last turn ended as failed by Codex's own account (not quota); "gui": the GUI / app-server broke.
    ("failure_source", "TEXT NOT NULL DEFAULT ''"),
    # dependencies
    ("dependency_policy", "TEXT NOT NULL DEFAULT 'all_success'"),
    # automatic recovery: retry_count = retries used so far (1 = the first retry is scheduled or ran)
    ("auto_retry_enabled", "INTEGER NOT NULL DEFAULT 1"), ("max_retries", "INTEGER NOT NULL DEFAULT 3"),
    ("retry_count", "INTEGER NOT NULL DEFAULT 0"), ("next_retry_at", "TEXT"),
    ("last_failure_kind", "TEXT NOT NULL DEFAULT ''"), ("last_failure_message", "TEXT NOT NULL DEFAULT ''"),
    ("last_exit_code", "INTEGER"), ("last_retry_at", "TEXT"),
    # JSON of the turn that is queued / running / to be retried: {"prompt", "resume_thread", "thread_id", "kind"}
    ("pending_turn", "TEXT"),
    # which GUI process claimed this queued task (atomic: only one claim succeeds), and when
    ("claimed_by", "TEXT"), ("claimed_at", "TEXT"),
    # 1: the worktree is created just before the first run (tasks that wait for dependencies)
    ("worktree_pending", "INTEGER NOT NULL DEFAULT 0"),
    # "<boot id>:<start ticks>" of the process `pid` refers to (see procinfo): a bare pid can be reused
    ("proc_identity", "TEXT"),
    # Context Efficiency. Frozen at task creation: the same Codex config goes into every thread/start|resume of the task.
    # allow_subagents defaults to 1 (Codex's own behaviour) so that tasks created before these settings keep running as they were.
    ("allow_subagents", "INTEGER NOT NULL DEFAULT 1"),
    ("tool_output_preset", "TEXT NOT NULL DEFAULT 'default'"), ("tool_output_limit", "INTEGER"),
    ("skills_preset", "TEXT NOT NULL DEFAULT 'default'"), ("skills_budget", "INTEGER"),
    ("tool_profile", "TEXT NOT NULL DEFAULT 'full'"), ("tool_profile_config", "TEXT NOT NULL DEFAULT ''"),
    ("tool_profile_check", "TEXT NOT NULL DEFAULT ''"),   # JSON: what the verification probe measured (or "" = not verified)
    ("cwd_subdir", "TEXT NOT NULL DEFAULT ''"),            # relative sub-directory of the worktree Codex runs in ("" = its root)
    ("stop_reason", "TEXT NOT NULL DEFAULT ''"),           # retry guard: why the last turn must not simply be retried
    ("compactions", "INTEGER NOT NULL DEFAULT 0"),
    ("last_cache_activity_at", "TEXT"),                    # last request that wrote to or read from the prompt cache
    ("last_request_input", "INTEGER"),                     # input size of the latest model request (not the thread total)
    ("long_context_ack", "TEXT NOT NULL DEFAULT ''"),      # long-context zone the user chose to Continue in
]

ATTEMPT_COLUMNS = (
    "task_id attempt_number trigger_kind started_at finished_at exit_code result failure_kind failure_message "
    "codex_thread_id was_resume service_tier reasoning_effort git_head git_status unpushed"
).split()

# The two conditions of the dependency policy "all_success", evaluated inside the UPDATE that acts on them so that the
# check and the state change are one atomic step: no parent can change in between, and two evaluators cannot both win.
_ALL_COMPLETED = ("NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id "
                  "WHERE d.task_id = tasks.id AND p.status != 'completed')")
_ANY_FAILED = ("EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id "
               "WHERE d.task_id = tasks.id AND p.status IN (%s))" % ", ".join(f"'{s}'" for s in sorted(DEPENDENCY_FAILED_STATUSES)))

# The same idea for scheduled instructions. `a` is the alias (or table name) of the scheduled_instructions row in the
# statement that uses them. The conditions are part of the UPDATE that acts on them, so a state change and the check
# that justified it are one atomic step (two evaluators, or the scheduler and a request, cannot both win).
_FAILED_LIST = ", ".join(f"'{s}'" for s in sorted(DEPENDENCY_FAILED_STATUSES))


def _si_deps_done(a: str) -> str:
    return ("NOT EXISTS (SELECT 1 FROM scheduled_instruction_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id "
            f"WHERE d.scheduled_instruction_id = {a}.id AND p.status != 'completed')")


def _si_deps_failed(a: str) -> str:
    return ("EXISTS (SELECT 1 FROM scheduled_instruction_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id "
            f"WHERE d.scheduled_instruction_id = {a}.id AND p.status IN ({_FAILED_LIST}))")


def _si_target_idle(a: str) -> str:
    """The target thread can take a turn now: its task is completed, has a recorded thread and a worktree."""
    return (f"EXISTS (SELECT 1 FROM tasks t WHERE t.id = {a}.task_id AND t.status = '{SCHEDULED_IDLE_TASK_STATUS}' "
            "AND COALESCE(t.codex_thread_id, '') != '' AND t.worktree_removed = 0 AND t.worktree_pending = 0)")


def _si_head_of_queue(a: str) -> str:
    """Nothing of the same task is running, and no older instruction of it is ready: the FIFO rule, per thread."""
    return (f"NOT EXISTS (SELECT 1 FROM scheduled_instructions o WHERE o.task_id = {a}.task_id AND "
            f"(o.status = 'running' OR (o.status = 'ready' AND (o.created_at < {a}.created_at OR "
            f"(o.created_at = {a}.created_at AND o.id < {a}.id)))))")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


TURN_COLUMNS = (
    "task_id turn session thread_id created_at input_tokens cached_input_tokens output_tokens "
    "cache_write_input_tokens reasoning_output_tokens total_json "
    "turn_id kind status model reasoning_effort started_at finished_at cache_hit_rate "
    "service_tier verbosity tool_profile requests max_request_input requests_json tool_calls large_tool_outputs "
    "tool_output_tokens_est compactions idle_before_seconds"
).split()

# kind: "turn" (an instruction) or "compact" (thread compaction, which also spends tokens).
TURN_MIGRATIONS = [
    ("turn_id", "TEXT"), ("kind", "TEXT NOT NULL DEFAULT 'turn'"), ("status", "TEXT NOT NULL DEFAULT 'completed'"),
    ("model", "TEXT"), ("reasoning_effort", "TEXT"), ("started_at", "TEXT"), ("finished_at", "TEXT"),
    ("cache_hit_rate", "REAL"),
    # what was REQUESTED for this turn (Standard/Fast can change inside one thread) and what the turn did
    ("service_tier", "TEXT"), ("verbosity", "TEXT"), ("tool_profile", "TEXT"),
    ("requests", "INTEGER"), ("max_request_input", "INTEGER"), ("requests_json", "TEXT"),
    ("tool_calls", "INTEGER"), ("large_tool_outputs", "INTEGER"), ("tool_output_tokens_est", "INTEGER"),
    ("compactions", "INTEGER"), ("idle_before_seconds", "INTEGER"),
]

LIMIT_COLUMNS = (
    "ts reason task_id plan_type limit_id primary_used_percent primary_window_mins primary_resets_at "
    "secondary_used_percent secondary_window_mins secondary_resets_at ordinary_usage_allowed reached_type "
    "available_resets"
).split()


class Database:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str, str, str], None]] = []
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, migrations in (("tasks", TASK_MIGRATIONS), ("turns", TURN_MIGRATIONS)):
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in migrations:
                if name not in have:
                    self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self._conn.close()

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def add_status_listener(self, listener: Callable[[str, str, str], None]) -> None:
        """listener(task_id, old_status, new_status) runs after every committed status change (never inside the lock)."""
        self._listeners.append(listener)

    def remove_status_listener(self, listener: Callable[[str, str, str], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self, task_id: str, old: str, new: str) -> None:
        for listener in self._listeners:
            try:
                listener(task_id, old, new)
            except Exception:  # a bad listener must not undo or hide a state change that is already committed
                log.exception("status listener failed for %s (%s -> %s)", task_id, old, new)

    def create_task(self, depends_on=(), **fields) -> dict:
        """Insert a task, and its dependency edges in the same transaction (a task is never visible half-wired)."""
        cols = [c for c in TASK_COLUMNS if c in fields]
        unknown = set(fields) - set(TASK_COLUMNS)
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        if fields.get("status") not in STATUSES:
            raise ValueError(f"invalid status: {fields.get('status')!r}")
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [fields[c] for c in cols],
            )
            if depends_on:
                self._insert_edges(fields["id"], list(depends_on))
        return self.get_task(fields["id"])

    # ---------- dependencies ----------

    def _edges(self) -> dict[str, list[str]]:
        """task id -> the ids it depends on (caller holds the lock)."""
        edges: dict[str, list[str]] = {}
        for r in self._conn.execute("SELECT task_id, depends_on_task_id FROM task_dependencies ORDER BY id"):
            edges.setdefault(r["task_id"], []).append(r["depends_on_task_id"])
        return edges

    def _insert_edges(self, task_id: str, parents: list[str], edges: Optional[dict] = None) -> None:
        """Add task_id -> each parent after checking the whole set. Raises DependencyError and writes nothing then."""
        if len(set(parents)) != len(parents):
            dup = next(p for p in parents if parents.count(p) > 1)
            raise DependencyError(f"duplicate dependency: {dup}", "duplicate")
        if task_id in parents:
            raise DependencyError(f"task {task_id} cannot depend on itself", "self")
        known = {r["id"] for r in self._conn.execute("SELECT id FROM tasks")}
        for p in parents:
            if p not in known:
                raise DependencyError(f"dependency task not found: {p}", "missing")
        edges = self._edges() if edges is None else edges
        have = set(edges.get(task_id, ()))
        for p in parents:
            if p in have:
                raise DependencyError(f"duplicate dependency: {p}", "duplicate")
            path = self._path(edges, p, task_id)
            if path:  # p already (transitively) depends on task_id: task_id -> p would close a loop
                raise DependencyError("dependency cycle: " + " -> ".join([task_id, *path]), "cycle")
        now = now_iso()
        for p in parents:
            self._conn.execute("INSERT INTO task_dependencies (task_id, depends_on_task_id, created_at) VALUES (?, ?, ?)",
                               (task_id, p, now))

    @staticmethod
    def _path(edges: dict[str, list[str]], start: str, target: str) -> Optional[list[str]]:
        """A chain start -> ... -> target following "depends on" edges, or None. Iterative DFS (no recursion limit)."""
        stack = [(start, [start])]
        seen = {start}
        while stack:
            node, path = stack.pop()
            if node == target:
                return path
            for nxt in edges.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, path + [nxt]))
        return None

    def add_dependencies(self, task_id: str, depends_on: list[str]) -> None:
        with self._lock, self._conn:
            self._insert_edges(task_id, list(depends_on))

    def replace_dependencies(self, task_id: str, depends_on: list[str]) -> None:
        """Set the full prerequisite list of a task in one transaction (the old edges do not count for the cycle check)."""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                raise KeyError(task_id)
            self._conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
            self._insert_edges(task_id, list(depends_on))

    def dependencies_of(self, task_id: str) -> list[str]:
        with self._lock:
            return [r["depends_on_task_id"] for r in self._conn.execute(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY id", (task_id,))]

    def dependents_of(self, task_id: str) -> list[str]:
        with self._lock:
            return [r["task_id"] for r in self._conn.execute(
                "SELECT task_id FROM task_dependencies WHERE depends_on_task_id = ? ORDER BY id", (task_id,))]

    def dependency_map(self) -> dict[str, list[str]]:
        with self._lock:
            return self._edges()

    def queue_if_ready(self, task_id: str) -> bool:
        """waiting_dependencies -> queued, only if every prerequisite is completed. True for exactly one caller."""
        return self._guarded(task_id, "waiting_dependencies", "queued", f"AND dependency_policy = 'all_success' AND {_ALL_COMPLETED}",
                             {"claimed_by": None, "claimed_at": None, "status_detail": ""})

    def block_if_failed(self, task_id: str, detail: str) -> bool:
        """waiting_dependencies -> blocked, only if some prerequisite failed, was stopped or is blocked itself."""
        return self._guarded(task_id, "waiting_dependencies", "blocked", f"AND {_ANY_FAILED}",
                             {"status_detail": detail, "finished_at": now_iso()})

    # ---------- atomic claims ----------

    def _guarded(self, task_id: str, old: str, new: str, extra_where: str, fields: dict) -> bool:
        """UPDATE ... WHERE id = ? AND status = old [AND extra]: one atomic step. True if this call made the change."""
        assignments = ", ".join(f"{k} = ?" for k in {**fields, "status": new})
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ? AND status = ? {extra_where}",
                [*fields.values(), new, task_id, old])
            changed = cur.rowcount == 1
        if changed:
            self._notify(task_id, old, new)
        return changed

    def transition(self, task_id: str, old: str, new: str, **fields) -> bool:
        """Compare-and-swap of the status: moves old -> new only if the task is still in `old`. False if it was not."""
        if not can_transition(old, new):
            raise InvalidTransition(f"{old} -> {new}")
        unknown = set(fields) - set(TASK_COLUMNS)
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        return self._guarded(task_id, old, new, "", fields)

    def claim_queued(self, task_id: str, owner: str) -> bool:
        """Claim a queued task for one runner. Of any number of concurrent callers exactly one gets True."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET claimed_by = ?, claimed_at = ? WHERE id = ? AND status = 'queued' AND claimed_by IS NULL",
                (owner, now_iso(), task_id))
            return cur.rowcount == 1

    def release_claim(self, task_id: str) -> None:
        self._execute("UPDATE tasks SET claimed_by = NULL, claimed_at = NULL WHERE id = ? AND status = 'queued'", (task_id,))

    def list_tasks_by_status(self, statuses) -> list[dict]:
        statuses = list(statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE status IN ({', '.join('?' * len(statuses))}) ORDER BY created_at, rowid",
                statuses).fetchall()
        return [dict(r) for r in rows]

    def due_retries(self, now: str) -> list[str]:
        with self._lock:
            return [r["id"] for r in self._conn.execute(
                "SELECT id FROM tasks WHERE status = 'retry_wait' AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY next_retry_at, rowid", (now,))]

    # ---------- attempts ----------

    def start_attempt(self, **fields) -> int:
        """A new attempt row; the number is task-wide and assigned in the same statement (no gaps, no duplicates)."""
        unknown = set(fields) - set(ATTEMPT_COLUMNS)
        if unknown or "task_id" not in fields:
            raise ValueError(f"bad attempt fields: {sorted(unknown)}")
        cols = [c for c in ATTEMPT_COLUMNS if c in fields and c != "task_id"]
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO task_attempts (task_id, attempt_number, {', '.join(cols)}) VALUES "
                f"(?, (SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM task_attempts WHERE task_id = ?), {', '.join('?' * len(cols))})",
                [fields["task_id"], fields["task_id"], *[fields[c] for c in cols]])
            return cur.lastrowid

    def update_attempt(self, attempt_id: int, **fields) -> None:
        unknown = set(fields) - set(ATTEMPT_COLUMNS)
        if unknown or not fields:
            raise ValueError(f"bad attempt fields: {sorted(unknown)}")
        self._execute(f"UPDATE task_attempts SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), attempt_id])

    def list_attempts(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_number", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def open_attempt(self, task_id: str) -> Optional[dict]:
        """The attempt that was started and never finished (the process or the GUI died), if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_attempts WHERE task_id = ? AND finished_at IS NULL ORDER BY attempt_number DESC LIMIT 1",
                (task_id,)).fetchone()
        return dict(row) if row else None

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC").fetchall()
        return [dict(r) for r in rows]

    def update_task(self, task_id: str, **fields) -> dict:
        if "status" in fields:
            raise ValueError("use set_status() to change status")
        return self._update(task_id, fields)

    def set_status(self, task_id: str, new_status: str, **fields) -> dict:
        """Change status, enforcing the transition table. Extra fields are written atomically."""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        old = task["status"]
        if not can_transition(old, new_status):
            raise InvalidTransition(f"{old} -> {new_status}")
        unknown = set(fields) - set(TASK_COLUMNS)
        if unknown or "id" in fields:
            raise ValueError(f"cannot update fields: {sorted(unknown | ({'id'} & set(fields)))}")
        # Compare-and-swap on the old status: a concurrent change (another evaluator, another process) cannot be overwritten.
        if not self._guarded(task_id, old, new_status, "", fields):
            raise InvalidTransition(f"{old} -> {new_status}: the task changed concurrently")
        return self.get_task(task_id)

    def _update(self, task_id: str, fields: dict) -> dict:
        unknown = set(fields) - set(TASK_COLUMNS)
        if unknown or "id" in fields:
            raise ValueError(f"cannot update fields: {sorted(unknown | ({'id'} & set(fields)))}")
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self._execute(f"UPDATE tasks SET {assignments} WHERE id = ?", [*fields.values(), task_id])
        return self.get_task(task_id)

    def touch_repo(self, path: str, when: str) -> None:
        self._execute(
            "INSERT INTO recent_repos (path, last_used) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET last_used = excluded.last_used",
            (path, when),
        )

    def recent_repos(self, limit: int = 10) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM recent_repos ORDER BY last_used DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r["path"] for r in rows]

    # ---------- scheduled instructions ----------

    def create_scheduled(self, task_id: str, prompt: str, service_tier: str, depends_on=(), created_by: str = "user") -> dict:
        """Insert an instruction and its dependency rows in one transaction. It starts as waiting_dependencies."""
        prompt = prompt.strip()
        if not prompt:
            raise ScheduledError("instruction is required", "empty")
        parents = [str(p) for p in depends_on]
        if len(set(parents)) != len(parents):
            raise ScheduledError(f"duplicate dependency: {next(p for p in parents if parents.count(p) > 1)}", "duplicate")
        if task_id in parents:  # it would wait for the very task it is meant to be sent to
            raise ScheduledError("an instruction cannot depend on its own target task (it waits for the thread to be idle anyway)", "self")
        with self._lock, self._conn:
            known = {r["id"] for r in self._conn.execute("SELECT id FROM tasks")}
            if task_id not in known:
                raise ScheduledError(f"task not found: {task_id}", "no_task")
            for p in parents:
                if p not in known:
                    raise ScheduledError(f"dependency task not found: {p}", "missing")
            cur = self._conn.execute(
                "INSERT INTO scheduled_instructions (task_id, prompt, status, service_tier, created_at, created_by) "
                "VALUES (?, ?, 'waiting_dependencies', ?, ?, ?)", (task_id, prompt, service_tier, now_iso(), created_by))
            sid = cur.lastrowid
            for p in parents:
                self._conn.execute("INSERT INTO scheduled_instruction_dependencies (scheduled_instruction_id, depends_on_task_id) "
                                   "VALUES (?, ?)", (sid, p))
        return self.get_scheduled(sid)

    def get_scheduled(self, sid: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM scheduled_instructions WHERE id = ?", (sid,)).fetchone()
        return dict(row) if row else None

    def list_scheduled(self, task_id: Optional[str] = None, statuses=None) -> list[dict]:
        """Oldest first (the order in which they are sent)."""
        where, params = [], []
        if task_id is not None:
            where.append("task_id = ?")
            params.append(task_id)
        if statuses is not None:
            statuses = list(statuses)
            where.append(f"status IN ({', '.join('?' * len(statuses))})")
            params += statuses
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM scheduled_instructions {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY created_at, id",
                params).fetchall()
        return [dict(r) for r in rows]

    def scheduled_dependencies(self, sid: int) -> list[str]:
        with self._lock:
            return [r["depends_on_task_id"] for r in self._conn.execute(
                "SELECT depends_on_task_id FROM scheduled_instruction_dependencies WHERE scheduled_instruction_id = ? "
                "ORDER BY rowid", (sid,))]

    def scheduled_dependency_failures(self, sid: int) -> list[dict]:
        """The dependencies of an instruction that will never complete by themselves (failed / stopped / blocked)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.id, p.name, p.status FROM scheduled_instruction_dependencies d JOIN tasks p ON p.id = d.depends_on_task_id "
                f"WHERE d.scheduled_instruction_id = ? AND p.status IN ({_FAILED_LIST}) ORDER BY d.rowid", (sid,)).fetchall()
        return [dict(r) for r in rows]

    def scheduled_counts(self) -> dict[str, dict]:
        """task id -> {"pending": instructions not sent yet, "ready": those that are ready}, for the dashboard."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, COUNT(*) AS pending, SUM(status = 'ready') AS ready FROM scheduled_instructions "
                f"WHERE status IN ({_in(SCHEDULED_WAITING)}) GROUP BY task_id").fetchall()
        return {r["task_id"]: {"pending": r["pending"], "ready": r["ready"] or 0} for r in rows}

    def advance_scheduled(self, sid: int, blocked_reason: str = "") -> str:
        """Move a not-yet-sent instruction to the state the database says it belongs in; returns its status.

        Each move is one conditional UPDATE (the status it leaves, and what justifies the move, are in the WHERE), so any
        number of callers can run this at once and an instruction only ever takes a step that is true when it is taken:
          dependency failed for good           -> blocked
          all completed, target thread idle    -> ready
          all completed, target thread busy    -> waiting_thread
          some dependency not completed (yet)  -> waiting_dependencies   (also back from ready: a completed task was run again)
        A dependency in retry_wait (or interrupted / waiting-for-quota) is not a failure: the instruction keeps waiting.
        """
        a = "scheduled_instructions"
        now = now_iso()
        waiting = _in(SCHEDULED_WAITING)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE {a} SET status = 'blocked', finished_at = ?, blocked_reason = ? WHERE id = ? AND status IN ({waiting}) "
                f"AND {_si_deps_failed(a)}", (now, blocked_reason, sid))
            self._conn.execute(
                f"UPDATE {a} SET status = 'ready', ready_at = ? WHERE id = ? AND status IN ('waiting_dependencies', 'waiting_thread') "
                f"AND {_si_deps_done(a)} AND {_si_target_idle(a)}", (now, sid))
            self._conn.execute(
                f"UPDATE {a} SET status = 'waiting_thread', ready_at = NULL WHERE id = ? AND status IN ('waiting_dependencies', 'ready') "
                f"AND {_si_deps_done(a)} AND NOT {_si_target_idle(a)}", (sid,))
            self._conn.execute(
                f"UPDATE {a} SET status = 'waiting_dependencies', ready_at = NULL WHERE id = ? AND status IN ('waiting_thread', 'ready') "
                f"AND NOT {_si_deps_done(a)}", (sid,))
        row = self.get_scheduled(sid)
        return row["status"] if row else ""

    def block_scheduled(self, sid: int, reason: str) -> bool:
        """Not-yet-sent -> blocked for a reason that will not heal by itself (e.g. the worktree was deleted)."""
        cur = self._execute(f"UPDATE scheduled_instructions SET status = 'blocked', finished_at = ?, blocked_reason = ? "
                            f"WHERE id = ? AND status IN ({_in(SCHEDULED_WAITING)})", (now_iso(), reason, sid))
        return cur.rowcount == 1

    def ready_scheduled(self) -> list[dict]:
        """The instruction each thread may send next: the oldest ready one of every task that has nothing running."""
        a = "s"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT s.* FROM scheduled_instructions s WHERE s.status = 'ready' AND {_si_head_of_queue(a)} ORDER BY s.created_at, s.id"
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_scheduled(self, sid: int, task_fields: dict, thread_id: str) -> bool:
        """ready -> running AND the target task completed -> queued, as ONE transaction. True for exactly one caller.

        The instruction row is only taken if it is still ready, nothing of the same task is running or ahead of it, every
        dependency is still completed and the thread is idle; the task row only if it is still `completed` on `thread_id`.
        If either UPDATE does not match, both are undone: the instruction stays as it was and nothing was sent. Because the
        same transaction makes the task `queued` (with the turn in pending_turn), no state exists in which the instruction
        is running while its thread still looks idle, so a second evaluator can never claim a second instruction for it.
        """
        a = "scheduled_instructions"
        unknown = set(task_fields) - set(TASK_COLUMNS)
        if unknown or "status" in task_fields:
            raise ValueError(f"cannot update fields: {sorted(unknown | ({'status'} & set(task_fields)))}")
        row = self.get_scheduled(sid)
        if row is None:
            return False
        assignments = ", ".join(f"{k} = ?" for k in {**task_fields, "status": "queued"})
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    f"UPDATE {a} SET status = 'running', started_at = ? WHERE id = ? AND status = 'ready' "
                    f"AND {_si_head_of_queue(a)} AND {_si_deps_done(a)} AND {_si_target_idle(a)}", (now_iso(), sid))
                if cur.rowcount != 1:
                    raise _Rollback()
                cur = self._conn.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ? AND status = '{SCHEDULED_IDLE_TASK_STATUS}' AND codex_thread_id = ?",
                    [*task_fields.values(), "queued", row["task_id"], thread_id])
                if cur.rowcount != 1:
                    raise _Rollback()
        except _Rollback:
            return False
        self._notify(row["task_id"], SCHEDULED_IDLE_TASK_STATUS, "queued")
        return True

    def finish_scheduled(self, sid: int, status: str, reason: str = "") -> bool:
        """running -> completed | failed (the outcome of the turn the instruction started). True if this call did it."""
        if status not in ("completed", "failed"):
            raise ValueError(f"bad final status: {status}")
        cur = self._execute("UPDATE scheduled_instructions SET status = ?, finished_at = ?, blocked_reason = ? "
                            "WHERE id = ? AND status = 'running'", (status, now_iso(), reason, sid))
        return cur.rowcount == 1

    def cancel_scheduled(self, sid: int) -> bool:
        """Cancel an instruction that has not been sent. The status condition is in the UPDATE: it cannot beat a claim."""
        cur = self._execute(
            f"UPDATE scheduled_instructions SET status = 'cancelled', finished_at = ?, blocked_reason = 'cancelled by the user' "
            f"WHERE id = ? AND status IN ({_in(SCHEDULED_CANCELLABLE)})", (now_iso(), sid))
        return cur.rowcount == 1

    # ---------- turns (token usage) ----------

    def add_turn(self, **fields) -> dict:
        unknown = set(fields) - set(TURN_COLUMNS)
        if unknown or "task_id" not in fields:
            raise ValueError(f"bad turn fields: {sorted(unknown)}")
        cols = [c for c in TURN_COLUMNS if c in fields]
        self._execute(
            f"INSERT INTO turns ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [fields[c] for c in cols],
        )
        return self.list_turns(fields["task_id"])[-1]

    def list_turns(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM turns WHERE task_id = ? ORDER BY turn", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_all_turns(self, since: Optional[str] = None) -> list[dict]:
        """Every turn of every task with the task's model, service tier and approval mode (for Efficiency).
        `since` (UTC ISO, like created_at) keeps only turns recorded at or after it."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.*, k.model AS task_model, k.service_tier AS task_tier, k.auto_approval AS task_auto "
                "FROM turns u JOIN tasks k ON k.id = u.task_id WHERE (? IS NULL OR u.created_at >= ?) "
                "ORDER BY u.task_id, u.turn", (since, since)).fetchall()
        return [dict(r) for r in rows]

    def latest_turns(self) -> dict[str, dict]:
        """The newest turn of every task that has one, keyed by task id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.* FROM turns t JOIN (SELECT task_id, MAX(turn) AS turn FROM turns GROUP BY task_id) m "
                "ON t.task_id = m.task_id AND t.turn = m.turn"
            ).fetchall()
        return {r["task_id"]: dict(r) for r in rows}

    # ---------- context events and settings ----------

    def add_context_event(self, task_id: str, kind: str, severity: str, message: str, ts: str,
                          turn: Optional[int] = None, data: Optional[dict] = None) -> None:
        self._execute("INSERT INTO context_events (task_id, turn, ts, kind, severity, message, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (task_id, turn, ts, kind, severity, message, json.dumps(data, ensure_ascii=False) if data else ""))

    def list_context_events(self, task_id: str, kinds: Optional[list] = None, limit: int = 300) -> list[dict]:
        """Oldest first; the newest `limit` events."""
        where, params = "WHERE task_id = ?", [task_id]
        if kinds:
            where += f" AND kind IN ({', '.join('?' * len(kinds))})"
            params += list(kinds)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM (SELECT * FROM context_events {where} ORDER BY id DESC LIMIT ?) ORDER BY id", [*params, limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d["data"]) if d["data"] else {}
            except ValueError:
                d["data"] = {}
            out.append(d)
        return out

    def count_context_events(self, task_id: str, kind: str) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM context_events WHERE task_id = ? AND kind = ?", (task_id, kind)).fetchone()[0]

    def get_settings(self, prefix: str = "") -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM app_settings WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_setting(self, key: str, value: str) -> None:
        self._execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def delete_setting(self, key: str) -> None:
        self._execute("DELETE FROM app_settings WHERE key = ?", (key,))

    # ---------- rate-limit history ----------

    def add_rate_limits(self, **fields) -> None:
        unknown = set(fields) - set(LIMIT_COLUMNS)
        if unknown or "ts" not in fields or "reason" not in fields:
            raise ValueError(f"bad rate-limit fields: {sorted(unknown)}")
        cols = [c for c in LIMIT_COLUMNS if c in fields]
        self._execute(
            f"INSERT INTO rate_limit_history ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [fields[c] for c in cols],
        )

    def list_rate_limits(self, limit: int = 200, task_id: Optional[str] = None) -> list[dict]:
        """Newest first."""
        where, params = ("WHERE task_id = ?", [task_id]) if task_id else ("", [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM rate_limit_history {where} ORDER BY id DESC LIMIT ?", [*params, limit]).fetchall()
        return [dict(r) for r in rows]
