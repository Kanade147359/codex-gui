import pytest

from app.models import (
    ACTIVE_STATUSES, STATUSES, TERMINAL_STATUSES, TRANSITIONS,
    branch_name, can_transition, slugify, worktree_path,
)


def test_slugify():
    assert slugify("Fix gossip retry!") == "fix-gossip-retry"
    assert slugify("  --A/B  c-- ") == "a-b-c"
    assert slugify("日本語のタスク") == "task"  # nothing ascii left -> fallback
    assert len(slugify("x" * 100)) <= 30
    assert not slugify("a" * 29 + " b").endswith("-")


def test_branch_name_format():
    assert branch_name("ab12cd34", "Implement gossip retry") == "codex-gui/ab12cd34-implement-gossip-retry"
    assert branch_name("ab12cd34", "???") == "codex-gui/ab12cd34-task"


def test_worktree_path(tmp_path):
    assert worktree_path(tmp_path, "/home/u/work/my repo", "abc") == tmp_path / "my_repo" / "abc"


def test_status_sets_are_consistent():
    from app.models import BUSY_STATUSES, WAITING_STATUSES
    assert ACTIVE_STATUSES | WAITING_STATUSES | TERMINAL_STATUSES == STATUSES
    assert not ACTIVE_STATUSES & TERMINAL_STATUSES and not WAITING_STATUSES & (ACTIVE_STATUSES | TERMINAL_STATUSES)
    assert BUSY_STATUSES == ACTIVE_STATUSES | WAITING_STATUSES
    assert set(TRANSITIONS) == STATUSES


@pytest.mark.parametrize("old,new", [
    ("queued", "starting"), ("starting", "running"), ("running", "completed"),
    ("running", "failed"), ("running", "stopped"), ("queued", "stopped"),
    ("running", "interrupted"), ("starting", "failed"),
    ("completed", "queued"), ("failed", "queued"), ("stopped", "queued"), ("interrupted", "queued"),  # next turn
])
def test_allowed_transitions(old, new):
    assert can_transition(old, new)


@pytest.mark.parametrize("old,new", [
    ("queued", "running"), ("queued", "completed"), ("running", "queued"),
    ("running", "running"), ("completed", "running"), ("failed", "completed"),
    ("stopped", "running"), ("interrupted", "failed"), ("bogus", "running"),
])
def test_rejected_transitions(old, new):
    assert not can_transition(old, new)


def test_waiting_for_quota_is_a_resting_status_that_can_be_run_again():
    from app.models import ACTIVE_STATUSES, STATUSES, TERMINAL_STATUSES, can_transition
    assert "waiting-for-quota" in STATUSES and "waiting-for-quota" in TERMINAL_STATUSES
    assert "waiting-for-quota" not in ACTIVE_STATUSES  # not active: nothing is retried behind the user's back
    assert can_transition("starting", "waiting-for-quota") and can_transition("running", "waiting-for-quota")
    assert can_transition("waiting-for-quota", "queued") and not can_transition("waiting-for-quota", "running")
    assert not can_transition("queued", "waiting-for-quota")


def test_sandboxes_never_include_the_full_access_mode():
    from app.models import ESCALATION, SANDBOXES
    assert "danger-full-access" not in SANDBOXES and "workspace-write" in SANDBOXES
    assert ESCALATION == ("low", "medium", "high")  # xhigh / max / ultra are explicit choices only
