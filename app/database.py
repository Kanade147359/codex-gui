"""SQLite persistence for tasks and recently used repositories."""
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .models import InvalidTransition, STATUSES, can_transition

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
CREATE TABLE IF NOT EXISTS recent_repos (
    path TEXT PRIMARY KEY,
    last_used TEXT NOT NULL
);
"""

TASK_COLUMNS = (
    "id name repository worktree branch base_ref base_sha prompt model reasoning_effort "
    "auto_approval status pid exit_code git_summary worktree_removed branch_deleted "
    "created_at started_at finished_at codex_thread_id last_turn_at "
    "service_tier model_verbosity web_search_enabled sandbox adaptive_reasoning context_guard writable_dirs feature_flags "
    "latest_input_tokens latest_cached_input_tokens latest_output_tokens context_tokens context_window "
    "five_hour_used_before five_hour_used_after weekly_used_before weekly_used_after quota_overlap "
    "last_prompt status_detail failure_source"
).split()

# Columns added after the first release: (name, definition) applied to databases that lack them.
# service_tier holds Codex's own id: "default" is Standard speed, "priority" is Fast.
TASK_MIGRATIONS = [
    ("codex_thread_id", "TEXT"), ("last_turn_at", "TEXT"),
    ("service_tier", "TEXT NOT NULL DEFAULT 'default'"),
    ("model_verbosity", "TEXT NOT NULL DEFAULT 'low'"),
    ("web_search_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("sandbox", "TEXT NOT NULL DEFAULT 'workspace-write'"),
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
]

TURN_COLUMNS = (
    "task_id turn session thread_id created_at input_tokens cached_input_tokens output_tokens "
    "cache_write_input_tokens reasoning_output_tokens total_json "
    "turn_id kind status model reasoning_effort started_at finished_at cache_hit_rate"
).split()

# kind: "turn" (an instruction) or "compact" (thread compaction, which also spends tokens).
TURN_MIGRATIONS = [
    ("turn_id", "TEXT"), ("kind", "TEXT NOT NULL DEFAULT 'turn'"), ("status", "TEXT NOT NULL DEFAULT 'completed'"),
    ("model", "TEXT"), ("reasoning_effort", "TEXT"), ("started_at", "TEXT"), ("finished_at", "TEXT"),
    ("cache_hit_rate", "REAL"),
]

LIMIT_COLUMNS = (
    "ts reason task_id plan_type limit_id primary_used_percent primary_window_mins primary_resets_at "
    "secondary_used_percent secondary_window_mins secondary_resets_at ordinary_usage_allowed reached_type "
    "available_resets"
).split()


class Database:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
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

    def create_task(self, **fields) -> dict:
        cols = [c for c in TASK_COLUMNS if c in fields]
        unknown = set(fields) - set(TASK_COLUMNS)
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        if fields.get("status") not in STATUSES:
            raise ValueError(f"invalid status: {fields.get('status')!r}")
        self._execute(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [fields[c] for c in cols],
        )
        return self.get_task(fields["id"])

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
        if not can_transition(task["status"], new_status):
            raise InvalidTransition(f"{task['status']} -> {new_status}")
        return self._update(task_id, {**fields, "status": new_status})

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

    def latest_turns(self) -> dict[str, dict]:
        """The newest turn of every task that has one, keyed by task id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.* FROM turns t JOIN (SELECT task_id, MAX(turn) AS turn FROM turns GROUP BY task_id) m "
                "ON t.task_id = m.task_id AND t.turn = m.turn"
            ).fetchall()
        return {r["task_id"]: dict(r) for r in rows}

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
