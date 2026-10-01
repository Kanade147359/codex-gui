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
CREATE TABLE IF NOT EXISTS recent_repos (
    path TEXT PRIMARY KEY,
    last_used TEXT NOT NULL
);
"""

TASK_COLUMNS = (
    "id name repository worktree branch base_ref base_sha prompt model reasoning_effort "
    "auto_approval status pid exit_code git_summary worktree_removed branch_deleted "
    "created_at started_at finished_at codex_thread_id last_turn_at"
).split()

# Columns added after the first release: (name, definition) applied to databases that lack them.
TASK_MIGRATIONS = [("codex_thread_id", "TEXT"), ("last_turn_at", "TEXT")]

TURN_COLUMNS = (
    "task_id turn session thread_id created_at input_tokens cached_input_tokens output_tokens "
    "cache_write_input_tokens reasoning_output_tokens total_json"
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
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        for name, definition in TASK_MIGRATIONS:
            if name not in have:
                self._execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

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
