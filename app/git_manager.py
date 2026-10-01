"""Thin async wrappers around the standard git CLI. No shell strings, only argv lists."""
import asyncio
from pathlib import Path
from typing import Optional

from .ssh_agent import child_env

MAX_OUTPUT_BYTES = 512 * 1024
MAX_UNTRACKED_FILES = 50


class GitError(Exception):
    pass


async def run_git(cwd, *args: str, check: bool = True) -> tuple[int, str, str]:
    env = child_env({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd), *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (FileNotFoundError, NotADirectoryError) as e:
        raise GitError(f"cannot run git in {cwd}: {e}") from e
    out, err = await proc.communicate()
    out_s, err_s = out.decode(errors="replace"), err.decode(errors="replace")
    if check and proc.returncode != 0:
        raise GitError((err_s or out_s).strip() or f"git {' '.join(args)} failed ({proc.returncode})")
    return proc.returncode, out_s, err_s


def _truncate(text: str) -> str:
    if len(text.encode()) <= MAX_OUTPUT_BYTES:
        return text
    return text.encode()[:MAX_OUTPUT_BYTES].decode(errors="ignore") + "\n... (truncated)\n"


async def repo_toplevel(path) -> Optional[str]:
    """Return the repository root containing `path`, or None if it is not a git work tree."""
    if not Path(path).is_dir():
        return None
    code, out, _ = await run_git(path, "rev-parse", "--show-toplevel", check=False)
    return out.strip() if code == 0 and out.strip() else None


async def is_git_repo(path) -> bool:
    return await repo_toplevel(path) is not None


async def resolve_commit(repo, ref: str) -> Optional[str]:
    """Resolve a branch/tag/commit to a full SHA, or None if it does not exist."""
    if not ref or ref.startswith("-"):
        return None
    code, out, _ = await run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.strip() if code == 0 and out.strip() else None


async def create_worktree(repo, path, branch: str, base_sha: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    await run_git(repo, "worktree", "add", "-b", branch, str(path), base_sha)


async def remove_worktree(repo, path, force: bool = False) -> None:
    args = ["worktree", "remove", *(["--force"] if force else []), str(path)]
    await run_git(repo, *args)


async def delete_branch(repo, branch: str, force: bool = False) -> None:
    await run_git(repo, "branch", "-D" if force else "-d", branch)


async def status_short(worktree) -> str:
    return (await run_git(worktree, "status", "--short", "--branch"))[1]


async def is_dirty(worktree) -> bool:
    return bool((await run_git(worktree, "status", "--porcelain"))[1].strip())


async def commit_count(worktree, base_sha: str) -> int:
    code, out, _ = await run_git(worktree, "rev-list", "--count", f"{base_sha}..HEAD", check=False)
    return int(out.strip()) if code == 0 and out.strip().isdigit() else 0


async def snapshot(worktree) -> dict:
    """Read-only picture of a worktree, recorded before a retry: `git status`, HEAD and the push state.

    Nothing here changes the repository. `pushed` is None when there is no upstream to compare with (never pushed, or no
    remote), else the number of local commits the upstream does not have yet.
    """
    _, status, _ = await run_git(worktree, "status", "--short", "--branch")
    code, head, _ = await run_git(worktree, "log", "-1", "--format=%h %s", check=False)
    code_up, ahead, _ = await run_git(worktree, "rev-list", "--count", "@{upstream}..HEAD", check=False)
    unpushed = int(ahead.strip()) if code_up == 0 and ahead.strip().isdigit() else None
    return {"status": status.strip(), "head": head.strip() if code == 0 else "", "unpushed": unpushed}


async def log_oneline(worktree, n: int = 10) -> str:
    return (await run_git(worktree, "log", "--oneline", f"-{n}"))[1]


async def untracked_files(worktree) -> list[str]:
    out = (await run_git(worktree, "ls-files", "--others", "--exclude-standard"))[1]
    return [line for line in out.splitlines() if line]


async def diff_stat(worktree, base_sha: str) -> str:
    """Stat of everything changed relative to the base commit (commits + working tree)."""
    return (await run_git(worktree, "diff", "--stat", base_sha))[1]


async def diff(worktree, base_sha: str) -> str:
    """Full diff relative to the base commit, including untracked files (without touching the index)."""
    parts = [(await run_git(worktree, "diff", base_sha))[1]]
    for name in (await untracked_files(worktree))[:MAX_UNTRACKED_FILES]:
        # --no-index exits with 1 when files differ, which is the expected case here.
        _, out, _ = await run_git(worktree, "diff", "--no-index", "--", "/dev/null", name, check=False)
        parts.append(out)
    return _truncate("".join(parts))


async def summary(worktree, base_sha: str) -> str:
    """One-word-ish state for the dashboard: 'dirty', '2 commits', 'dirty · 1 commit', 'clean'."""
    if not Path(worktree).is_dir():
        return "no worktree"
    try:
        dirty = await is_dirty(worktree)
        commits = await commit_count(worktree, base_sha)
    except GitError:
        return "unknown"
    parts = []
    if dirty:
        parts.append("dirty")
    if commits:
        parts.append(f"{commits} commit{'s' if commits != 1 else ''}")
    return " · ".join(parts) or "clean"


async def commit_all(worktree, message: str) -> str:
    await run_git(worktree, "add", "-A")
    code, out, err = await run_git(worktree, "commit", "-m", message, check=False)
    if code != 0:
        raise GitError((err or out).strip() or "git commit failed")
    return out


async def push(worktree, branch: str) -> str:
    _, out, err = await run_git(worktree, "push", "-u", "origin", branch)
    return (out + err).strip()


async def list_refs(repo) -> dict:
    """Everything that can serve as a base ref: local branches, other worktrees, remote branches, tags."""
    fmt = "%(refname)\t%(refname:short)\t%(objectname:short)\t%(contents:subject)"
    out = (await run_git(repo, "for-each-ref", "--sort=-committerdate", f"--format={fmt}",
                         "refs/heads", "refs/remotes", "refs/tags"))[1]
    refs = {"branches": [], "remotes": [], "tags": []}
    for line in out.splitlines():
        full, short, sha, subject = (line.split("\t") + ["", "", "", ""])[:4]
        item = {"name": short, "sha": sha, "subject": subject}
        if full.startswith("refs/heads/"):
            refs["branches"].append(item)
        elif full.startswith("refs/remotes/") and not full.endswith("/HEAD"):
            refs["remotes"].append(item)
        elif full.startswith("refs/tags/"):
            refs["tags"].append(item)
    refs["remotes"], refs["tags"] = refs["remotes"][:200], refs["tags"][:50]

    worktrees = []
    wt_out = (await run_git(repo, "worktree", "list", "--porcelain"))[1]
    for block in wt_out.split("\n\n")[1:]:  # the first block is the main working tree itself
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        if "worktree" not in fields or "bare" in block.split():
            continue
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        sha = fields.get("HEAD", "")[:10]
        worktrees.append({"path": fields["worktree"], "branch": branch, "sha": sha, "ref": branch or sha})
    refs["worktrees"] = worktrees

    _, current, _ = await run_git(repo, "branch", "--show-current", check=False)
    refs["current"] = current.strip()
    names = {b["name"] for b in refs["branches"]}
    refs["default"] = "main" if "main" in names else refs["current"] or (refs["branches"][0]["name"] if refs["branches"] else "HEAD")
    return refs
