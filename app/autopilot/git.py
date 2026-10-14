"""Fixed-SHA snapshots and a single writer's integration refs; never modify live checkouts."""

import asyncio
import hashlib
from pathlib import Path
import os
import signal

from app.git_manager import GitError
from app.ssh_agent import child_env
from .policy import owns


async def git(root, *args, check=True):
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        *args,
        env=child_env({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def read(stream):
        chunks = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return chunks.decode(errors="replace")
            chunks.extend(chunk)
            if len(chunks) > 2 * 1024 * 1024:
                raise GitError("Git scope/output exceeds 2 MiB; partition or inspect explicitly")

    async def collect():
        out, err = await asyncio.gather(read(proc.stdout), read(proc.stderr))
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(collect(), 300)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    if check and proc.returncode != 0:
        raise GitError(err.strip() or f"Git operation failed ({proc.returncode})")
    return proc.returncode, out, err


async def sha(root, ref="HEAD"):
    _, out, _ = await git(root, "rev-parse", "--verify", ref + "^{commit}")
    value = out.strip()
    if len(value) != 40:
        raise GitError("full SHA required")
    return value


async def scope(root, base, head):
    _, tree, _ = await git(root, "rev-parse", head + "^{tree}")
    _, diff, _ = await git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", base, head)
    return dict(
        base_sha=base, head_sha=head, tree_sha=tree.strip(), diff_digest=hashlib.sha256(diff.encode()).hexdigest()
    )


async def changed_paths(root, base, head="HEAD"):
    _, committed, _ = await git(root, "diff", "--name-only", "-z", "--no-renames", base, head)
    _, dirty, _ = await git(root, "diff", "--name-only", "-z", "--no-renames", "HEAD")
    _, untracked, _ = await git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return set((committed + dirty + untracked).split("\0")) - {""}


async def submit_head(task, spec, permission):
    root = task["worktree"]
    paths = await changed_paths(root, task["base_sha"])
    if any(not owns(p, spec["paths"]) for p in paths):
        raise ValueError("candidate contains changes outside approved edit ownership")
    _, dirty, _ = await git(root, "status", "--porcelain")
    if dirty:
        if not permission:
            raise ValueError("owned changes need commit permission")
        # This is a workflow-owned isolated worktree. Include only explicitly owned changed files.
        await git(root, "add", "--", *sorted(paths))
        await git(root, "commit", "-m", "Autopilot: submit owned candidate")
    return await sha(root)


async def snapshot(repository, head, base, destination):
    destination = Path(destination)
    if destination.exists():
        if await sha(destination) != head:
            raise ValueError("snapshot identity mismatch")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    await git(destination.parent, "clone", "--no-hardlinks", "--no-checkout", "--", repository, str(destination))
    await git(destination, "checkout", "--detach", head)
    await sha(destination, base)  # ensure the entire review scope is available
    # Delete remotes so even the controller's clone has no accidental push destination.
    await git(destination, "remote", "remove", "origin")
    return destination


async def integrate(root, heads, operation):
    """Record deterministic merge order; already contained and stacked commits are skipped."""
    steps = []
    for head in heads:
        rc, _, _ = await git(root, "merge-base", "--is-ancestor", head, "HEAD", check=False)
        if rc == 0:
            steps.append(dict(head=head, action="already_contained"))
            continue
        start = await sha(root)
        rc, out, err = await git(
            root, "merge", "--no-edit", "--no-ff", head, "-m", f"Autopilot integration {operation}: {head}", check=False
        )
        steps.append(dict(head=head, action="merge", start_sha=start, exit_status=rc))
        if rc:
            _, conflicts, _ = await git(root, "diff", "--name-only", "--diff-filter=U")
            return dict(
                state="conflict",
                steps=steps,
                conflicts=conflicts.splitlines(),
                remaining=heads[heads.index(head) + 1 :],
            )
    return dict(state="merged", steps=steps, head_sha=await sha(root))
