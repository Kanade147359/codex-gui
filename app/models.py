"""Task statuses, the allowed transitions between them, and naming helpers."""
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# "queued": ready to run (dependencies met, or the user gave another instruction) but a runner has not started it yet.
# The scheduler (or the request that queued it) claims it atomically, so a queued task is started exactly once.
ACTIVE_STATUSES = frozenset({"queued", "starting", "running"})
# Waiting for the scheduler, no process involved. "waiting_dependencies": prerequisite tasks are not all completed;
# "retry_wait": an unexpected stop, to be retried at next_retry_at (same worktree, same Codex thread).
WAITING_STATUSES = frozenset({"waiting_dependencies", "retry_wait"})
# "waiting-for-quota": Codex reported that the included usage is exhausted. Nothing is retried and nothing
# falls back to another billing path; the user re-runs the instruction when the quota is available again.
# "blocked": a prerequisite failed / was stopped / is blocked itself; the task is never started on its own.
TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "interrupted", "waiting-for-quota", "blocked"})
STATUSES = ACTIVE_STATUSES | WAITING_STATUSES | TERMINAL_STATUSES
# A task in one of these has work scheduled or running: its worktree must not be touched and it takes no new instruction.
BUSY_STATUSES = ACTIVE_STATUSES | WAITING_STATUSES
# A prerequisite in one of these will never complete by itself: its dependents become "blocked".
# (waiting-for-quota and interrupted are paused, not final: the user can resume them, so dependents keep waiting.)
DEPENDENCY_FAILED_STATUSES = frozenset({"failed", "stopped", "blocked"})
DEPENDENCY_POLICIES = ("all_success",)  # all_terminal etc. can be added later

# Scheduled instructions: a follow-up turn for the EXISTING Codex thread of one task, held back until the tasks it
# depends on are completed AND that task's thread is idle. Not a task dependency: nothing here ever starts a task.
#   waiting_dependencies -> waiting_thread -> ready -> running -> completed
# A dependency that failed for good (failed / stopped / blocked) makes it `blocked`; Cancel is possible until it runs.
SCHEDULED_WAITING = ("waiting_dependencies", "waiting_thread", "ready")   # not sent yet; the scheduler re-evaluates these
SCHEDULED_CANCELLABLE = SCHEDULED_WAITING + ("blocked",)
SCHEDULED_OPEN = SCHEDULED_WAITING + ("running",)                         # still occupy the task's FIFO queue
SCHEDULED_STATUSES = frozenset(SCHEDULED_CANCELLABLE + ("running", "completed", "cancelled", "failed"))
# The only task status in which the thread counts as idle. `failed`, `stopped`, `interrupted` and `waiting-for-quota`
# are deliberately not: they need a decision by the user (retry, resume, new session), and sending a follow-up into
# them on our own would defeat a Stop and make one failed instruction cascade into the next.
SCHEDULED_IDLE_TASK_STATUS = "completed"

# "interrupted" = the GUI shut down while the task was active (see TaskManager.shutdown).
# A finished task goes back to "queued" when it is given another turn (codex exec resume, or a
# new session): one task keeps one worktree and, normally, one Codex thread across many turns.
TRANSITIONS = {
    "waiting_dependencies": {"queued", "blocked", "stopped", "failed"},
    "blocked": {"queued", "waiting_dependencies", "stopped"},
    "queued": {"starting", "stopped", "failed", "interrupted"},
    "starting": {"running", "stopped", "failed", "interrupted", "waiting-for-quota", "retry_wait"},
    "running": {"completed", "failed", "stopped", "interrupted", "waiting-for-quota", "retry_wait"},
    "retry_wait": {"queued", "stopped", "failed"},
    "completed": {"queued"},
    "failed": {"queued"},
    "stopped": {"queued"},
    "interrupted": {"queued"},
    "waiting-for-quota": {"queued"},
}

# The CLI decides which efforts a model supports (the UI offers those); we only keep the argv value sane.
EFFORT_RE = re.compile(r"^[a-z]{1,16}$")
# Codex's own ids ("default" = standard speed, "priority" = Fast): the catalog says which a model offers.
SERVICE_TIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
VERBOSITIES = ("low", "medium", "high")
# danger-full-access is deliberately absent: the GUI never starts Codex without its sandbox.
SANDBOXES = ("read-only", "workspace-write")
# Tried in this order by "Retry with ..."; xhigh / max / ultra are only ever used when the user picks them.
ESCALATION = ("low", "medium", "high")


class InvalidTransition(ValueError):
    pass


def can_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, set())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(seconds_from_now: float = 0.0) -> str:
    """UTC ISO time with milliseconds ("2026-10-01T21:43:12.250Z"): strings of this form sort like the times they name.
    Used where sub-second precision matters (the retry clock); now_iso() stays second-precise for display."""
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
