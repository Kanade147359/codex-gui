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
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS recent_repos (
    path TEXT PRIMARY KEY,
    last_used TEXT NOT NULL
);
"""

TASK_COLUMNS = (
    "id name repository worktree branch base_ref base_sha prompt model reasoning_effort "
    "auto_approval status pid exit_code git_summary worktree_removed branch_deleted "
    "created_at started_at finished_at"
).split()


class Database:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

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
