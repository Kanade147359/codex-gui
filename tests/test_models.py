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
    assert ACTIVE_STATUSES | TERMINAL_STATUSES == STATUSES
    assert not ACTIVE_STATUSES & TERMINAL_STATUSES
    assert set(TRANSITIONS) == STATUSES


@pytest.mark.parametrize("old,new", [
    ("queued", "starting"), ("starting", "running"), ("running", "completed"),
    ("running", "failed"), ("running", "stopped"), ("queued", "stopped"),
    ("running", "interrupted"), ("starting", "failed"),
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
