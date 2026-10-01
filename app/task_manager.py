"""Creates tasks (branch + worktree), runs codex processes in parallel, and keeps the DB in sync."""
import asyncio
import os
from pathlib import Path
from typing import Optional

from . import git_manager as git
from .codex_runner import CodexRunner, pid_is_codex, terminate_process
from .config import Settings
from .database import Database
from .logstore import TaskLog, read_log
from .models import (
    ACTIVE_STATUSES, EFFORT_RE, TERMINAL_STATUSES,
    branch_name, make_task_id, now_iso, worktree_path,
)

READER_DRAIN_SECONDS = 5.0


class TaskError(Exception):
    """A problem the API should report to the user. `code` lets the UI react (e.g. "dirty")."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class TaskManager:
    def __init__(self, settings: Settings, db: Database, runner: Optional[CodexRunner] = None):
        self.settings = settings
        self.db = db
        self.runner = runner or CodexRunner(settings.codex_bin)
        self._jobs: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._stop_requested: set[str] = set()
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False
        self._slots = asyncio.Semaphore(settings.max_concurrent) if settings.max_concurrent > 0 else None

    # ---------- lookup ----------

    def log_path(self, task_id: str) -> Path:
        return self.settings.logs_dir / f"{task_id}.jsonl"

    def get(self, task_id: str) -> dict:
        task = self.db.get_task(task_id)
        if task is None:
            raise TaskError("task not found", 404)
        return task

    def read_log(self, task_id: str, offset: int = 0) -> tuple[list[dict], int]:
        self.get(task_id)
        return read_log(self.log_path(task_id), offset)

    # ---------- creation ----------

    async def create_task(self, *, repository: str, base_ref: str = "main", name: str = "", prompt: str,
                          model: str = "", reasoning_effort: str = "default",
                          auto_approval: bool = True) -> dict:
        prompt = prompt.strip()
        if not prompt:
            raise TaskError("prompt is required")
        name = name.strip() or (prompt.splitlines()[0][:60])
        model = model.strip()
        if model.startswith("-"):
            raise TaskError("invalid model name")
        if reasoning_effort != "default" and not EFFORT_RE.match(reasoning_effort):
            raise TaskError(f"invalid reasoning effort: {reasoning_effort}")
        base_ref = base_ref.strip() or "main"

        repo_input = Path(repository.strip()).expanduser()
        if not repository.strip() or not repo_input.is_dir():
            raise TaskError(f"not a directory: {repository}")
        repo = await git.repo_toplevel(repo_input)
        if repo is None:
            raise TaskError(f"not a git repository: {repository}")
        base_sha = await git.resolve_commit(repo, base_ref)
        if base_sha is None:
            raise TaskError(f"base ref not found: {base_ref}")

        task_id = make_task_id()
        branch = branch_name(task_id, name)
        wt = worktree_path(self.settings.worktrees_dir, repo, task_id)
        # Several tasks may be created in the same repo at once; git's own locks (config, refs)
        # can make concurrent `worktree add` calls fail, so creation is serialized per repository.
        async with self._repo_locks.setdefault(repo, asyncio.Lock()):
            try:
                await git.create_worktree(repo, wt, branch, base_sha)
            except git.GitError as e:
                raise TaskError(f"git worktree add failed: {e}") from e

        task = self.db.create_task(
            id=task_id, name=name, repository=repo, worktree=str(wt), branch=branch,
            base_ref=base_ref, base_sha=base_sha, prompt=prompt, model=model,
            reasoning_effort=reasoning_effort, auto_approval=int(auto_approval),
            status="queued", git_summary="clean", created_at=now_iso(),
        )
        self.db.touch_repo(repo, now_iso())
        job = asyncio.create_task(self._run(task_id))
        job.add_done_callback(lambda _: self._jobs.pop(task_id, None))
        self._jobs[task_id] = job
        return task

    # ---------- running ----------

    async def _run(self, task_id: str) -> None:
        log = TaskLog(self.log_path(task_id))
        try:
            if self._slots:
                await self._slots.acquire()
            try:
                await self._run_process(task_id, log)
            finally:
                if self._slots:
                    self._slots.release()
        except asyncio.CancelledError:
            raise  # stop() of a queued task; it already wrote the final status
        except Exception as e:  # never leave a task stuck in an active status
            log.add_system(f"internal error: {e!r}")
            if self.db.get_task(task_id)["status"] in ACTIVE_STATUSES:
                self.db.set_status(task_id, "failed", finished_at=now_iso())
        finally:
            log.close()
            self._jobs.pop(task_id, None)
            self._procs.pop(task_id, None)
            self._stop_requested.discard(task_id)

    async def _run_process(self, task_id: str, log: TaskLog) -> None:
        task = self.db.set_status(task_id, "starting")
        try:
            proc = await self.runner.spawn(task)
        except OSError as e:
            log.add_system(f"failed to start codex: {e}")
            self.db.set_status(task_id, "failed", finished_at=now_iso())
            return
        self._procs[task_id] = proc
        log.add_system(f"started pid {proc.pid}: {' '.join(self.runner.build_command(task))}")
        self.db.set_status(task_id, "running", pid=proc.pid, started_at=now_iso())
        if task_id in self._stop_requested:  # stop arrived while starting
            asyncio.create_task(terminate_process(proc, self.settings.stop_grace_seconds))

        async def pump(stream, write):
            while True:
                line = await stream.readline()
                if not line:
                    return
                write(line.decode(errors="replace"))

        async def feed_prompt():
            try:
                proc.stdin.write(task["prompt"].encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        readers = [asyncio.create_task(pump(proc.stdout, log.add_stdout)),
                   asyncio.create_task(pump(proc.stderr, log.add_stderr)),
                   asyncio.create_task(feed_prompt())]
        refresher = asyncio.create_task(self._refresh_loop(task_id))
        try:
            code = await proc.wait()
            # A grandchild may keep the pipes open after codex exits; don't wait for it forever.
            await asyncio.wait(readers, timeout=READER_DRAIN_SECONDS)
        finally:
            refresher.cancel()
            for r in readers:
                r.cancel()

        if task_id in self._stop_requested:
            status = "interrupted" if self._shutting_down else "stopped"
        else:
            status = "completed" if code == 0 else "failed"
        log.add_system(f"process exited with code {code} -> {status}")
        self.db.set_status(task_id, status, exit_code=code, finished_at=now_iso())
        await self.refresh_git_summary(task_id)

    async def _refresh_loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.git_refresh_seconds)
            await self.refresh_git_summary(task_id)

    async def refresh_git_summary(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task and not task["worktree_removed"]:
            self.db.update_task(task_id, git_summary=await git.summary(task["worktree"], task["base_sha"]))

    # ---------- stop ----------

    async def stop(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task["status"] not in ACTIVE_STATUSES:
            raise TaskError(f"task is {task['status']}, not active", 409)
        if task["status"] == "queued":
            job = self._jobs.get(task_id)
            if job:
                job.cancel()
            return self.db.set_status(task_id, "stopped", finished_at=now_iso())
        self._stop_requested.add(task_id)
        proc = self._procs.get(task_id)
        if proc:  # don't block the HTTP request for the SIGTERM -> SIGKILL grace period
            asyncio.create_task(terminate_process(proc, self.settings.stop_grace_seconds))
        return task

    async def shutdown(self) -> None:
        """Called when the GUI exits: codex children are stopped (we cannot re-attach to them later)."""
        self._shutting_down = True
        for task_id in list(self._jobs):
            task = self.db.get_task(task_id)
            if task and task["status"] in ACTIVE_STATUSES:
                try:
                    await self.stop(task_id)
                except TaskError:
                    pass
        jobs = list(self._jobs.values())
        if jobs:
            await asyncio.wait(jobs, timeout=self.settings.stop_grace_seconds + READER_DRAIN_SECONDS + 5)

    # ---------- restart recovery ----------

    def recover(self) -> list[str]:
        """Tasks left active in the DB by a previous GUI process cannot be re-attached: mark them interrupted."""
        fixed = []
        for task in self.db.list_tasks():
            if task["status"] not in ACTIVE_STATUSES:
                continue
            log = TaskLog(self.log_path(task["id"]))
            note = "GUI restarted while this task was active; marking it interrupted."
            if pid_is_codex(task["pid"]):
                note += f" (pid {task['pid']} still looks alive and was left running; it can no longer be tracked)"
            log.add_system(note)
            log.close()
            self.db.set_status(task["id"], "interrupted", finished_at=now_iso())
            fixed.append(task["id"])
        return fixed

    # ---------- git views and operations ----------

    def _require_worktree(self, task: dict) -> str:
        if task["worktree_removed"] or not os.path.isdir(task["worktree"]):
            raise TaskError("worktree no longer exists", 409, "no_worktree")
        return task["worktree"]

    def _require_inactive(self, task: dict) -> None:
        if task["status"] in ACTIVE_STATUSES:
            raise TaskError("task is still active; stop it first", 409)

    async def git_info(self, task_id: str) -> dict:
        task = self.get(task_id)
        if task["worktree_removed"] or not os.path.isdir(task["worktree"]):
            return {"available": False, "branch": task["branch"]}
        wt, base = task["worktree"], task["base_sha"]
        try:
            status, stat, diff, log = await asyncio.gather(
                git.status_short(wt), git.diff_stat(wt, base), git.diff(wt, base), git.log_oneline(wt, 10))
        except git.GitError as e:
            return {"available": False, "branch": task["branch"], "error": str(e)}
        return {"available": True, "branch": task["branch"], "status": status,
                "diff_stat": stat, "diff": diff, "log": log}

    async def commit(self, task_id: str, message: str) -> str:
        task = self.get(task_id)
        self._require_inactive(task)
        wt = self._require_worktree(task)
        message = message.strip() or f"codex-gui: {task['name']}"
        try:
            out = await git.commit_all(wt, message)
        except git.GitError as e:
            raise TaskError(str(e), 409) from e
        await self.refresh_git_summary(task_id)
        return out

    async def push(self, task_id: str) -> str:
        task = self.get(task_id)
        self._require_inactive(task)
        wt = self._require_worktree(task)
        try:
            return await git.push(wt, task["branch"])
        except git.GitError as e:
            raise TaskError(str(e), 409) from e

    async def delete_worktree(self, task_id: str, force: bool = False) -> dict:
        task = self.get(task_id)
        self._require_inactive(task)
        if task["worktree_removed"]:
            raise TaskError("worktree already removed", 409)
        wt = task["worktree"]
        if os.path.isdir(wt):
            if not force and await git.is_dirty(wt):
                raise TaskError("worktree has uncommitted changes", 409, "dirty")
            try:
                await git.remove_worktree(task["repository"], wt, force=force)
            except git.GitError as e:
                raise TaskError(str(e), 409) from e
        else:
            await git.run_git(task["repository"], "worktree", "prune", check=False)
        # The branch is intentionally kept (and with it every commit); delete it separately.
        return self.db.update_task(task_id, worktree_removed=1, git_summary="worktree removed")

    async def delete_branch(self, task_id: str, force: bool = False) -> dict:
        task = self.get(task_id)
        self._require_inactive(task)
        if not task["worktree_removed"]:
            raise TaskError("delete the worktree first", 409)
        if task["branch_deleted"]:
            raise TaskError("branch already deleted", 409)
        try:
            await git.delete_branch(task["repository"], task["branch"], force=force)
        except git.GitError as e:
            code = "unmerged" if "not fully merged" in str(e) else ""
            raise TaskError(str(e), 409, code) from e
        return self.db.update_task(task_id, branch_deleted=1)

    # ---------- views ----------

    def counts(self, tasks: list[dict]) -> dict:
        counts = {}
        for t in tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return counts

    @staticmethod
    def is_terminal(task: dict) -> bool:
        return task["status"] in TERMINAL_STATUSES
