"""HTTP routes: two HTML pages plus a small JSON API (polled by static/app.js)."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .models import REASONING_EFFORTS
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


class CommitRequest(BaseModel):
    message: str = ""


def api_error(e: TaskError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"message": str(e), "code": e.code})


# ---------- pages ----------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", {"efforts": REASONING_EFFORTS})


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
    tasks = m.db.list_tasks()
    return {"tasks": tasks, "counts": m.counts(tasks)}


@router.post("/api/tasks")
async def create_task(request: Request, body: NewTask):
    try:
        return await manager(request).create_task(**body.model_dump())
    except TaskError as e:
        raise api_error(e)


@router.get("/api/repos")
async def recent_repos(request: Request):
    return {"repos": manager(request).db.recent_repos()}


@router.get("/api/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    try:
        return manager(request).get(task_id)
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
