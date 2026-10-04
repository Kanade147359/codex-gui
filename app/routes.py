"""HTTP routes: two HTML pages plus a small JSON API (polled by static/app.js)."""
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .attachments import AttachmentError, MAX_IMAGE_BYTES, MAX_IMAGES, MAX_MESSAGE_BYTES, MAX_DIMENSION, MAX_PIXELS

from . import agents_audit, agents_md, completion, ctx_config
from . import git_manager as git
from .codex_login import LoginError
from .dependency_graph import graph_view
from .fs_browser import BrowseError, list_dir
from .task_manager import TaskError, TaskManager

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

router = APIRouter()


def manager(request: Request) -> TaskManager:
    return request.app.state.manager


@router.get("/api/attachments/limits")
async def attachment_limits():
    return dict(max_image_bytes=MAX_IMAGE_BYTES, max_message_bytes=MAX_MESSAGE_BYTES, max_images=MAX_IMAGES,
                max_dimension=MAX_DIMENSION, max_pixels=MAX_PIXELS, media_types=["image/png", "image/jpeg", "image/webp"])


@router.post("/api/attachments")
async def upload_attachment(request: Request, filename: str = "image.png"):
    # Read a bounded raw body, not multipart/Base64, and do not trust MIME or filename extensions.
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
            raise HTTPException(413, detail={"message": "画像1枚の容量上限は10 MiBです。", "code": "invalid_attachment"})
        data.extend(chunk)
    try:
        return await run_in_threadpool(manager(request).attachments.save, bytes(data), filename)
    except AttachmentError as e:
        raise HTTPException(400, detail={"message": str(e), "code": "invalid_attachment"}) from e
    except OSError as e:
        raise HTTPException(500, detail={"message": "画像を保存できません。保存先の権限・空き容量を確認してください。", "code": "attachment_storage"}) from e


@router.get("/api/attachments/{image_id}")
async def get_attachment(request: Request, image_id: str):
    store = manager(request).attachments
    try:
        row = store.get(image_id)
        path = store.path(row)
        if not path.is_file():
            raise AttachmentError("保存済み画像が見つかりません。")
    except AttachmentError as e:
        raise HTTPException(404, detail={"message": str(e), "code": "invalid_attachment"}) from e
    return FileResponse(path, media_type=row["media_type"], headers={"X-Content-Type-Options": "nosniff",
                                                                    "Cache-Control": "private, max-age=31536000, immutable"})


class NewTask(BaseModel):
    repository: str
    base_ref: str = "main"
    name: str = ""
    prompt: str
    attachment_ids: list[str] = []
    model: str = ""
    reasoning_effort: str = "default"
    auto_approval: bool = True
    # Optimization defaults: Standard speed, low verbosity, no web search, sandboxed, adaptive retries and the
    # context guard on. (reasoning_effort "default" = whatever Codex / the model defaults to; the form sends "low".)
    service_tier: str = "default"
    model_verbosity: str = "low"
    web_search: Union[bool, Literal["cached", "live", "disabled"]] = "cached"  # True = live, False = disabled
    sandbox: str = "workspace-write"
    network_access: bool = True  # network inside the workspace-write sandbox (ignored for read-only)
    adaptive_reasoning: bool = True
    context_guard: bool = True
    writable_dirs: str = ""
    feature_flags: str = ""
    # Run after other tasks: the ids this task waits for (empty = start immediately). Policy: all_success.
    depends_on: list[str] = []
    dependency_policy: str = "all_success"
    # Automatic recovery from an unexpected stop. None = the server's defaults (on, 3 retries).
    auto_retry: Optional[bool] = None
    max_retries: Optional[int] = None
    # Context Efficiency. Frozen for the task. Presets are the GUI's own; "default" = Codex's behaviour.
    tool_output: str = "default"               # default | conservative (8000) | balanced (16000) | large (32000)
    tool_output_limit: Optional[int] = None    # an explicit token limit instead of a preset
    skills: str = "default"                    # default | economy (2000) | balanced (4000) | large (8000): catalog budget
    skills_budget: Optional[int] = None
    allow_subagents: bool = False              # nested agents are OFF unless the task needs them
    tool_profile: str = "full"                 # full | development | minimal
    cwd_subdir: str = ""                       # Advanced: run Codex in a sub-directory of the worktree
    completion_contract: Optional[dict] = None


class CompletionOverride(BaseModel):
    outcome: Literal["success", "blocked"]
    reason: str
    confirm: bool = False


class LoginRequest(BaseModel):
    method: Literal["browser", "device"] = "browser"


class ResumeRequest(BaseModel):
    task_ids: Optional[list[str]] = None


class Instruction(BaseModel):
    prompt: str
    attachment_ids: list[str] = []
    reasoning_effort: Optional[str] = None  # an explicit "Retry with ..." choice; omitted = unchanged
    service_tier: Optional[str] = None      # speed of THIS turn: "standard" (Send Standard) / "fast" (Send Fast); omitted = the task's


class ScheduledInstruction(BaseModel):
    prompt: str
    attachment_ids: list[str] = []
    depends_on: list[str] = []              # tasks that must be completed first; empty = send when the thread is idle
    service_tier: Optional[str] = "standard"  # THIS instruction's speed: "standard" / "fast" (never inherited from the last turn)


class ToolProfileChange(BaseModel):
    profile: str
    confirm: bool = False  # the user accepted "Changing tool configuration may reduce prompt cache reuse"


class ToolProfileVerify(BaseModel):
    repository: str
    profile: str
    force: bool = False


class ContextSettings(BaseModel):
    values: dict[str, int]


class CommitRequest(BaseModel):
    message: str = ""


class RetryRequest(BaseModel):
    confirm_over_limit: bool = False  # the user confirmed retrying past the automatic retry limit


class AutoRetryRequest(BaseModel):
    enabled: Optional[bool] = None
    max_retries: Optional[int] = None


class DependenciesRequest(BaseModel):
    depends_on: list[str]


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


def context_options() -> dict:
    """Preset tables for the New Task form (the values are the GUI's own presets, not Codex's)."""
    return {
        "tool_output": [{"id": k, "label": ctx_config.TOOL_OUTPUT_LABELS[k], "limit": v} for k, v in ctx_config.TOOL_OUTPUT_PRESETS.items()],
        "skills": [{"id": k, "label": ctx_config.SKILLS_LABELS[k], "budget": v} for k, v in ctx_config.SKILLS_PRESETS.items()],
        "tool_profiles": [{"id": k, "label": ctx_config.TOOL_PROFILE_LABELS[k]} for k in ctx_config.TOOL_PROFILES],
        "default_tool_output": "default", "default_skills": "default", "default_tool_profile": "full", "default_allow_subagents": False,
    }


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


@router.get('/dependencies', response_class=HTMLResponse)
async def graph_page(request: Request):
    return TEMPLATES.TemplateResponse(request, 'graph.html', {})


@router.get('/api/dependency-graph')
async def graph_data(request: Request):
    return graph_view(manager(request).db)


@router.get("/api/options")
async def options(request: Request):
    """Model choices (from the codex CLI) and recently used repositories for the New Task form."""
    m = manager(request)
    request.app.state.completion_validation = completion.validation_capability()
    return {**await request.app.state.catalog.get(), "repos": m.db.recent_repos(),
            "backend": m.settings.backend, "subscription_only": m.settings.subscription_only,
            "default_auto_retry": m.settings.default_auto_retry, "default_max_retries": m.settings.default_max_retries,
            "context_warn_percent": m.settings.context_warn_percent, "context_efficiency": context_options(),
            "completion_approval": m.completion_approval_settings(),
            "completion_validation": request.app.state.completion_validation}


class CompletionApprovalSettings(BaseModel):
    auto_approve_verified_success: bool


@router.get("/api/completion/settings")
async def get_completion_settings(request: Request):
    return manager(request).completion_approval_settings()


@router.put("/api/completion/settings")
async def put_completion_settings(request: Request, body: CompletionApprovalSettings):
    return manager(request).set_completion_approval(body.auto_approve_verified_success)


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


# ---------- Codex sign-in ----------

def login_error(e: LoginError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"message": str(e), "code": "codex_login"})


@router.get("/api/codex/account")
async def codex_account(request: Request):
    """Whether Codex is signed in (and as whom), plus the state of a sign-in in progress."""
    try:
        return await manager(request).codex_login.status()
    except LoginError as e:
        raise login_error(e)


@router.post("/api/codex/login")
async def codex_login_start(request: Request, body: LoginRequest):
    """Start signing in to Codex with ChatGPT. The answer carries the page the browser has to open (and, for the
    device-code method, the code to type there)."""
    try:
        return await manager(request).codex_login.start(body.method)
    except LoginError as e:
        raise login_error(e)


@router.post("/api/codex/login/cancel")
async def codex_login_cancel(request: Request):
    return await manager(request).codex_login.cancel()


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


@router.get("/api/efficiency")
async def get_efficiency(request: Request, period: Literal["today", "7d", "lifetime"] = "lifetime"):
    """Efficiency of all tasks over a period: credit/API-equivalent estimates, not real subscription savings."""
    return manager(request).efficiency_for(period)


@router.post("/api/tasks/resume-interrupted")
async def resume_interrupted(request: Request, body: ResumeRequest):
    """Continue interrupted tasks in their existing Codex threads (all of them unless task_ids is given)."""
    return await manager(request).resume_interrupted(body.task_ids)


@router.post("/api/tasks/{task_id}/messages")
async def send_instruction(request: Request, task_id: str, body: Instruction):
    """Additional instruction: continues the task's existing Codex session (codex exec resume)."""
    try:
        return await manager(request).send_instruction(task_id, body.prompt, body.reasoning_effort, body.service_tier, body.attachment_ids)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/completion/checks")
async def rerun_completion(request: Request, task_id: str):
    try:
        return await manager(request).rerun_completion(task_id)
    except TaskError as e:
        raise api_error(e)


@router.put("/api/tasks/{task_id}/completion/contract")
async def completion_contract(request: Request, task_id: str, body: dict):
    try:
        return manager(request).set_completion_contract(task_id, body)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/completion/override")
async def override_completion(request: Request, task_id: str, body: CompletionOverride):
    try:
        return manager(request).override_completion(task_id, body.outcome, body.reason, body.confirm)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/dependents/cancel")
async def cancel_dependents(request: Request, task_id: str):
    try:
        return await manager(request).cancel_dependents(task_id)
    except TaskError as e:
        raise api_error(e)


@router.get("/api/tasks/{task_id}/scheduled")
async def list_scheduled(request: Request, task_id: str):
    try:
        return {"scheduled": manager(request).scheduled_instructions(task_id)}
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/scheduled")
async def schedule_instruction(request: Request, task_id: str, body: ScheduledInstruction):
    """Reserve an instruction for the task's existing Codex thread: sent once the dependencies are completed and the thread is idle."""
    try:
        return await manager(request).schedule_instruction(task_id, body.prompt, body.depends_on, body.service_tier, attachment_ids=body.attachment_ids)
    except TaskError as e:
        raise api_error(e)


@router.delete("/api/tasks/{task_id}/scheduled/{scheduled_id}")
async def cancel_scheduled(request: Request, task_id: str, scheduled_id: int):
    """Cancel a scheduled instruction that has not been sent yet."""
    try:
        return manager(request).cancel_scheduled(task_id, scheduled_id)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/new-session")
async def start_new_session(request: Request, task_id: str, body: Instruction):
    try:
        return await manager(request).start_new_session(task_id, body.prompt, body.attachment_ids)
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


@router.get("/api/tasks/{task_id}/attempts")
async def get_attempts(request: Request, task_id: str):
    """Every run of the task (first run, instructions, retries), apart from the token-usage turns."""
    try:
        return {"attempts": manager(request).attempts(task_id)}
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/retry")
async def retry_task(request: Request, task_id: str, body: Optional[RetryRequest] = None):
    """Retry a failed or stopped task (or end a retry wait now): same worktree, same Codex thread."""
    try:
        return await manager(request).retry_task(task_id, bool(body and body.confirm_over_limit))
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/auto-retry")
async def set_auto_retry(request: Request, task_id: str, body: AutoRetryRequest):
    try:
        return await manager(request).set_auto_retry(task_id, body.enabled, body.max_retries)
    except TaskError as e:
        raise api_error(e)


@router.put("/api/tasks/{task_id}/dependencies")
async def set_dependencies(request: Request, task_id: str, body: DependenciesRequest):
    """Replace the prerequisites of a task that has not started. Cycles, self and duplicate dependencies are refused."""
    try:
        return await manager(request).set_dependencies(task_id, body.depends_on)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/run-anyway")
async def run_anyway(request: Request, task_id: str):
    """Start a waiting or blocked task without its prerequisites."""
    try:
        return await manager(request).run_anyway(task_id)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/retry-dependencies")
async def retry_dependencies(request: Request, task_id: str, body: Optional[RetryRequest] = None):
    """For a blocked task: retry its failed or stopped prerequisites and wait for them again."""
    try:
        return await manager(request).retry_failed_dependencies(task_id, bool(body and body.confirm_over_limit))
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


# ---------- Context Efficiency ----------

@router.get("/api/context/preview")
async def context_preview(request: Request, repository: str, cwd_subdir: str = "", skills: str = "default",
                          skills_budget: Optional[int] = None):
    """AGENTS.md health check (read-only), the skills catalog and the model's tool-output cap for a repository."""
    try:
        return await manager(request).context_preview(repository, cwd_subdir, skills=skills, skills_budget=skills_budget)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tool-profiles/verify")
async def verify_tool_profile(request: Request, body: ToolProfileVerify):
    """Measure, with the real Codex and no model, how many tools the model can reach with a profile (vs Codex's default)."""
    try:
        return await manager(request).verify_tool_profile(body.repository, body.profile, body.force)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/tool-profile")
async def change_tool_profile(request: Request, task_id: str, body: ToolProfileChange):
    try:
        return await manager(request).change_tool_profile(task_id, body.profile, body.confirm)
    except TaskError as e:
        raise api_error(e)


@router.post("/api/tasks/{task_id}/context-ack")
async def context_ack(request: Request, task_id: str):
    """[Continue] in the long-context banner: only remembers the choice. Nothing is compacted or changed."""
    try:
        return manager(request).acknowledge_long_context(task_id)
    except TaskError as e:
        raise api_error(e)


@router.get("/api/tasks/{task_id}/agents-audit")
async def task_agents_audit(request: Request, task_id: str):
    """The AGENTS.md chain Codex reads for this task's actual working directory. Read-only."""
    m = manager(request)
    try:
        task = m.get(task_id)
    except TaskError as e:
        raise api_error(e)
    cwd = ctx_config.task_cwd(task)
    if task["worktree_removed"] or not Path(cwd).is_dir():
        raise HTTPException(status_code=409, detail={"message": "worktree no longer exists", "code": "no_worktree"})
    cfg = await m.effective_config(cwd) or {}
    return agents_audit.audit(
        cwd, max_bytes=cfg.get("project_doc_max_bytes") if isinstance(cfg.get("project_doc_max_bytes"), int) else None,
        markers=cfg.get("project_root_markers") if isinstance(cfg.get("project_root_markers"), list) else None,
        fallbacks=tuple(cfg.get("project_doc_fallback_filenames") or ()))


@router.get("/api/context/settings")
async def get_context_settings(request: Request):
    return manager(request).ctx.threshold_values()


@router.put("/api/context/settings")
async def put_context_settings(request: Request, body: ContextSettings):
    try:
        return manager(request).ctx.set_thresholds(body.values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"message": str(e), "code": "invalid"})


@router.delete("/api/context/settings")
async def reset_context_settings(request: Request):
    return manager(request).ctx.reset_thresholds()


@router.get("/api/tasks/{task_id}/context-events")
async def context_events(request: Request, task_id: str, kind: str = "", limit: int = 200):
    try:
        manager(request).get(task_id)
    except TaskError as e:
        raise api_error(e)
    kinds = [k for k in kind.split(",") if k] or None
    return {"events": manager(request).db.list_context_events(task_id, kinds, min(max(limit, 1), 1000))}
