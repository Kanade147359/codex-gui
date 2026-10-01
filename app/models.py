"""Task statuses, the allowed transitions between them, and naming helpers."""
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_STATUSES = frozenset({"queued", "starting", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "interrupted"})
STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

# "interrupted" = the GUI went away while the task was active (see TaskManager.recover).
# A finished task goes back to "queued" when it is given another turn (codex exec resume, or a
# new session): one task keeps one worktree and, normally, one Codex thread across many turns.
TRANSITIONS = {
    "queued": {"starting", "stopped", "failed", "interrupted"},
    "starting": {"running", "stopped", "failed", "interrupted"},
    "running": {"completed", "failed", "stopped", "interrupted"},
    "completed": {"queued"},
    "failed": {"queued"},
    "stopped": {"queued"},
    "interrupted": {"queued"},
}

# The CLI decides which efforts a model supports (the UI offers those); we only keep the argv value sane.
EFFORT_RE = re.compile(r"^[a-z]{1,16}$")


class InvalidTransition(ValueError):
    pass


def can_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, set())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def make_task_id() -> str:
    return uuid.uuid4().hex[:8]


def slugify(text: str, max_len: int = 30) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].strip("-")
    return slug or "task"


def branch_name(task_id: str, name: str) -> str:
    return f"codex-gui/{task_id}-{slugify(name)}"


def worktree_path(root: Path, repository: str, task_id: str) -> Path:
    repo_dir = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(repository).name) or "repo"
    return Path(root) / repo_dir / task_id
