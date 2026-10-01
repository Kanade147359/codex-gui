import asyncio
import subprocess

import pytest

from app import git_manager as git


def run(coro):
    return asyncio.run(coro)


def sha(repo, ref="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True, check=True).stdout.strip()


def test_repo_toplevel(git_repo, tmp_path):
    assert run(git.repo_toplevel(git_repo)) == str(git_repo.resolve())
    sub = git_repo / "sub"
    sub.mkdir()
    assert run(git.repo_toplevel(sub)) == str(git_repo.resolve())
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(git.repo_toplevel(plain)) is None
    assert run(git.repo_toplevel(tmp_path / "missing")) is None
    assert run(git.is_git_repo(git_repo)) is True
    assert run(git.is_git_repo(plain)) is False


def test_resolve_commit(git_repo):
    assert run(git.resolve_commit(git_repo, "main")) == sha(git_repo)
    assert run(git.resolve_commit(git_repo, sha(git_repo)[:8])) == sha(git_repo)
    assert run(git.resolve_commit(git_repo, "nope")) is None
    assert run(git.resolve_commit(git_repo, "")) is None
    assert run(git.resolve_commit(git_repo, "--output=/tmp/x")) is None  # never treated as an option


def test_create_worktree(git_repo, tmp_path):
    wt = tmp_path / "wts" / "repo" / "t1"
    run(git.create_worktree(git_repo, wt, "codex-gui/t1-x", sha(git_repo)))
    assert (wt / "README.md").exists()
    assert subprocess.run(["git", "-C", str(wt), "branch", "--show-current"], capture_output=True, text=True).stdout.strip() == "codex-gui/t1-x"
    # same branch again -> GitError, nothing half-created
    with pytest.raises(git.GitError):
        run(git.create_worktree(git_repo, tmp_path / "wts" / "repo" / "t2", "codex-gui/t1-x", sha(git_repo)))


def test_summary_diff_and_commit(git_repo, tmp_path):
    base = sha(git_repo)
    wt = tmp_path / "wt"
    run(git.create_worktree(git_repo, wt, "b1", base))
    assert run(git.summary(wt, base)) == "clean"

    (wt / "new.txt").write_text("untracked\n")
    (wt / "README.md").write_text("# changed\n")
    assert run(git.summary(wt, base)) == "dirty"
    diff = run(git.diff(wt, base))
    assert "+# changed" in diff and "new.txt" in diff and "+untracked" in diff  # untracked included
    assert "README.md" in run(git.diff_stat(wt, base))
    status = run(git.status_short(wt))
    assert "?? new.txt" in status and " M README.md" in status

    run(git.commit_all(wt, "work"))
    assert run(git.summary(wt, base)) == "1 commit"
    assert not run(git.is_dirty(wt))
    assert "work" in run(git.log_oneline(wt, 5))
    # diff vs base still shows committed changes
    assert "+# changed" in run(git.diff(wt, base))
    (wt / "more.txt").write_text("x")
    assert run(git.summary(wt, base)) == "dirty · 1 commit"
    assert run(git.summary(tmp_path / "gone", base)) == "no worktree"


def test_commit_without_changes_raises(git_repo, tmp_path):
    wt = tmp_path / "wt"
    run(git.create_worktree(git_repo, wt, "b1", sha(git_repo)))
    with pytest.raises(git.GitError):
        run(git.commit_all(wt, "nothing"))


def test_remove_worktree_and_delete_branch(git_repo, tmp_path):
    base = sha(git_repo)
    wt = tmp_path / "wt"
    run(git.create_worktree(git_repo, wt, "b1", base))
    (wt / "dirty.txt").write_text("x")
    with pytest.raises(git.GitError):
        run(git.remove_worktree(git_repo, wt))  # git itself refuses without --force
    run(git.remove_worktree(git_repo, wt, force=True))
    assert not wt.exists()
    branches = subprocess.run(["git", "-C", str(git_repo), "branch", "--list", "b1"], capture_output=True, text=True).stdout
    assert "b1" in branches  # branch survives worktree removal
    run(git.delete_branch(git_repo, "b1"))  # no commits of its own -> fully merged
    assert "b1" not in subprocess.run(["git", "-C", str(git_repo), "branch"], capture_output=True, text=True).stdout


def test_list_refs(git_repo, tmp_path):
    git_run = lambda *a: subprocess.run(["git", "-C", str(git_repo), *a], check=True, capture_output=True)
    git_run("branch", "feature")
    git_run("tag", "v1")
    wt = tmp_path / "wt"
    git_run("worktree", "add", "-b", "codex-gui/t1-x", str(wt))
    detached = tmp_path / "wt2"
    git_run("worktree", "add", "--detach", str(detached))

    refs = run(git.list_refs(git_repo))
    assert {b["name"] for b in refs["branches"]} == {"main", "feature", "codex-gui/t1-x"}
    assert refs["current"] == "main" and refs["default"] == "main"
    assert [t["name"] for t in refs["tags"]] == ["v1"] and refs["remotes"] == []
    by_ref = {w["ref"]: w for w in refs["worktrees"]}
    assert by_ref["codex-gui/t1-x"]["path"] == str(wt.resolve())  # main working tree is not listed
    assert any(w["branch"] == "" and len(w["ref"]) == 10 for w in refs["worktrees"])  # detached -> sha
    # every offered ref resolves, so it is usable as a base ref
    for r in [b["name"] for b in refs["branches"]] + [w["ref"] for w in refs["worktrees"]] + ["v1"]:
        assert run(git.resolve_commit(git_repo, r))


def test_list_refs_default_falls_back_to_current_branch(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    assert run(git.list_refs(repo))["default"] == "trunk"
