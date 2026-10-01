"""HTTP routes: two HTML pages plus a small JSON API (polled by static/app.js)."""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import agents_md
from . import git_manager as git
from .fs_browser import BrowseError, list_dir
from .task_manager import TaskError, TaskManager

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

router = APIRouter()


def manager(request: Request) -> TaskManager:
    return request.app.state.manager


class NewTask(BaseModel):
    repository: str
    base_ref: str = "main"
    name: str = ""
    prompt: str
    model: str = ""
    reasoning_effort: str = "default"
    auto_approval: bool = True
    # Optimization defaults: Standard speed, low verbosity, no web search, sandboxed, adaptive retries and the
    # context guard on. (reasoning_effort "default" = whatever Codex / the model defaults to; the form sends "low".)
    service_tier: str = "default"
    model_verbosity: str = "low"
    web_search: bool = False
    sandbox: str = "workspace-write"
    adaptive_reasoning: bool = True
    context_guard: bool = True
    writable_dirs: str = ""
    feature_flags: str = ""


class Instruction(BaseModel):
    prompt: str
    reasoning_effort: Optional[str] = None  # an explicit "Retry with ..." choice; omitted = unchanged


class CommitRequest(BaseModel):
    message: str = ""


def api_error(e) -> HTTPException:
    """TaskError and AgentsError both carry (message, status, code)."""
    return HTTPException(status_code=e.status, detail={"message": str(e), "code": e.code})


class AgentsSave(BaseModel):
    content: str
    path: str = agents_md.FILENAME
    expected_sha: Optional[str] = None  # sha256 of what the editor loaded; "" = the file did not exist


class RepoAgentsSave(AgentsSave):
    repository: str


async def repo_root(repository: str) -> str:
    """The main checkout of a repository (scope "repository"). A linked worktree is refused: that is a task's copy."""
    path = Path(repository.strip()).expanduser()
    root = await git.repo_toplevel(path) if repository.strip() else None
    if root is None:
        raise agents_md.AgentsError(f"not a git repository: {repository}")
    await agents_md.main_worktree_only(root)
    return root


def worktree_root(request: Request, task_id: str) -> str:
    """The task's own worktree (scope "task worktree")."""
    try:
        task = manager(request).get(task_id)
    except TaskError as e:
        raise api_error(e)
    if task["worktree_removed"] or not Path(task["worktree"]).is_dir():
        raise HTTPException(status_code=409, detail={"message": "worktree no longer exists", "code": "no_worktree"})
    return task["worktree"]


# ---------- pages ----------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", {})


@router.get("/agents", response_class=HTMLResponse)
async def repo_agents_page(request: Request, repository: str = ""):
    return TEMPLATES.TemplateResponse(request, "agents.html", {"scope": "repository", "repository": repository, "task_id": ""})


@router.get("/tasks/{task_id}/agents", response_class=HTMLResponse)
async def task_agents_page(request: Request, task_id: str):
    try:
        task = manager(request).get(task_id)
    except TaskError as e:
        raise api_error(e)
    return TEMPLATES.TemplateResponse(request, "agents.html", {"scope": "task", "repository": task["repository"], "task_id": task_id})


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_page(request: Request, task_id: str):
    try:
        manager(request).get(task_id)
    except TaskError as e:
        raise api_error(e)
    return TEMPLATES.TemplateResponse(request, "task.html", {"task_id": task_id})


# ---------- API ----------

@router.get("/api/tasks")
async def list_tasks(request: Request):
    m = manager(request)
    tasks = m.list_tasks_view()
    return {"tasks": tasks, "counts": m.counts(tasks)}


@router.post("/api/tasks")
async def create_task(request: Request, body: NewTask):
    try:
        return await manager(request).create_task(**body.model_dump())
    except TaskError as e:
        raise api_error(e)


@router.get("/api/options")
async def options(request: Request):
    """Model choices (from the codex CLI) and recently used repositories for the New Task form."""
    m = manager(request)
    return {**await request.app.state.catalog.get(), "repos": m.db.recent_repos(),
            "backend": m.settings.backend, "subscription_only": m.settings.subscription_only,
            "context_warn_percent": m.settings.context_warn_percent}


@router.get("/api/fs")
async def browse(path: str = "", hidden: bool = False):
    try:
        return list_dir(path, hidden)
    except BrowseError as e:
        raise HTTPException(status_code=400, detail={"message": str(e), "code": ""})


@router.get("/api/refs")
async def refs(repository: str):
    """Base ref choices for a repository (branches, worktrees, remote branches, tags)."""
    repo = await git.repo_toplevel(Path(repository.strip()).expanduser())
    if repo is None:
        raise HTTPException(status_code=400, detail={"message": f"not a git repository: {repository}", "code": ""})
    return {"repository": repo, **await git.list_refs(repo)}


@router.get("/api/repos")
async def recent_repos(request: Request):
    return {"repos": manager(request).db.recent_repos()}


@router.get("/api/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    m = manager(request)
    try:
        return m.present_task(m.get(task_id))
    except TaskError as e:
        raise api_error(e)


@router.get("/api/tasks/{task_id}/log")
async def get_log(request: Request, task_id: str, offset: int = 0):
    try:
        entries, next_offset = manager(request).read_log(task_id, max(offset, 0))
    except TaskError as e:
        raise api_error(e)
    return {"entries": entries, "offset": next_offset}


@router.get("/api/tasks/{task_id}/git")
async def get_git(request: Request, task_id: str):
    try:
        return await manager(request).git_info(task_id)
    except TaskError as e:
        raise api_error(e)


@router.get("/api/tasks/{task_id}/usage")
async def get_usage(request: Request, task_id: str):
    try:
        return manager(request).usage(task_id)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/messages")
async def send_instruction(request: Request, task_id: str, body: Instruction):
    """Additional instruction: continues the task's existing Codex session (codex exec resume)."""
    try:
        return await manager(request).send_instruction(task_id, body.prompt, body.reasoning_effort)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/new-session")
async def start_new_session(request: Request, task_id: str, body: Instruction):
    try:
        return await manager(request).start_new_session(task_id, body.prompt)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/compact")
async def compact_task(request: Request, task_id: str):
    """Manual compaction of the task's Codex thread (app-server backend only)."""
    try:
        return await manager(request).compact(task_id)
    except TaskError as e:
        raise api_error(e)


@router.get("/api/limits")
async def limits(request: Request, refresh: bool = False):
    """Codex usage windows for the dashboard. Display only: nothing is steered by it."""
    return await manager(request).rate_limits(force=refresh)


@router.get("/api/limits/history")
async def limits_history(request: Request, limit: int = 200, task_id: str = ""):
    return {"history": manager(request).db.list_rate_limits(min(max(limit, 1), 1000), task_id or None)}


@router.post("/api/tasks/{task_id}/stop")
async def stop_task(request: Request, task_id: str):
    try:
        return await manager(request).stop(task_id)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/commit")
async def commit_task(request: Request, task_id: str, body: CommitRequest):
    try:
        return {"output": await manager(request).commit(task_id, body.message)}
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/push")
async def push_task(request: Request, task_id: str):
    try:
        return {"output": await manager(request).push(task_id)}
    except TaskError as e:
        raise api_error(e)


@router.delete("/api/tasks/{task_id}/worktree")
async def delete_worktree(request: Request, task_id: str, force: bool = False):
    try:
        return await manager(request).delete_worktree(task_id, force)
    except TaskError as e:
        raise api_error(e)


@router.delete("/api/tasks/{task_id}/branch")
async def delete_branch(request: Request, task_id: str, force: bool = False):
    try:
        return await manager(request).delete_branch(task_id, force)
    except TaskError as e:
        raise api_error(e)


# ---------- AGENTS.md ----------

@router.get("/api/repo-info")
async def repo_info(repository: str):
    """Git cleanliness and whether AGENTS.md exists (and nested ones), for the dashboard and the New Task form."""
    root = await git.repo_toplevel(Path(repository.strip()).expanduser()) if repository.strip() else None
    if root is None:
        raise HTTPException(status_code=400, detail={"message": f"not a git repository: {repository}", "code": ""})
    return await agents_md.repo_info(root)


async def _agents_read(root: str, path: str) -> dict:
    return {**await agents_md.read(root, path), "root": root, "files": await agents_md.find_all(root)}


@router.get("/api/agents-md")
async def get_repo_agents(repository: str, path: str = agents_md.FILENAME):
    """AGENTS.md of the main repository checkout."""
    try:
        return await _agents_read(await repo_root(repository), path)
    except agents_md.AgentsError as e:
        raise api_error(e)


@router.put("/api/agents-md")
async def put_repo_agents(body: RepoAgentsSave):
    try:
        root = await repo_root(body.repository)
        return await agents_md.write(root, body.path, body.content, body.expected_sha)
    except agents_md.AgentsError as e:
        raise api_error(e)


@router.get("/api/agents-md/diff")
async def repo_agents_diff(repository: str, path: str = agents_md.FILENAME):
    try:
        return await agents_md.diff(await repo_root(repository), path)
    except agents_md.AgentsError as e:
        raise api_error(e)


@router.get("/api/tasks/{task_id}/agents-md")
async def get_task_agents(request: Request, task_id: str, path: str = agents_md.FILENAME):
    """AGENTS.md of one task's worktree: a separate file from the repository's."""
    try:
        return await _agents_read(worktree_root(request, task_id), path)
    except agents_md.AgentsError as e:
        raise api_error(e)


@router.put("/api/tasks/{task_id}/agents-md")
async def put_task_agents(request: Request, task_id: str, body: AgentsSave):
    try:
        return await agents_md.write(worktree_root(request, task_id), body.path, body.content, body.expected_sha)
    except agents_md.AgentsError as e:
        raise api_error(e)


@router.get("/api/tasks/{task_id}/agents-md/diff")
async def task_agents_diff(request: Request, task_id: str, path: str = agents_md.FILENAME):
    try:
        return await agents_md.diff(worktree_root(request, task_id), path)
    except agents_md.AgentsError as e:
        raise api_error(e)
