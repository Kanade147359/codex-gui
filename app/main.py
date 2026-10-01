"""FastAPI app factory. Run with: uvicorn --factory app.main:create_app"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth_routes
from .appserver import AppServerClient
from .auth import AuthRequired, AuthService, require_login
from .catalog import ModelCatalog
from .codex_runner import CodexRunner
from .config import Settings
from .database import Database
from .routes import router
from . import ssh_agent
from .task_manager import TaskManager

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# script-src has no 'unsafe-inline': the pages carry no inline scripts. style-src needs it for the usage bars.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def same_origin(request: Request) -> bool:
    """CSRF guard for state-changing requests: a browser always sends Origin on a cross-site POST, and it must name this
    host (the Host header, or X-Forwarded-Host when a reverse proxy rewrote it). No Origin = not a browser form/fetch."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = urlsplit(origin).netloc
    return bool(host) and host in {request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")}



def create_app(settings: Optional[Settings] = None, runner: Optional[CodexRunner] = None,
               app_server: Optional[AppServerClient] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    if not settings.auth_enabled:
        log.warning("login is disabled (CODEX_GUI_AUTH=0): anyone who can reach this port controls Codex on this machine")
    elif db.count_users() == 0:
        log.warning("login is enabled but there are no users yet: run  .venv/bin/python -m app.users add <name>")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager.recover()
        app.state.manager.scheduler.start()
        diagnostics = None
        if ssh_agent.diagnostics_enabled():
            # Background: ssh -T may take seconds and must not delay startup.
            repos = list(dict.fromkeys([str(Path.cwd()), *db.recent_repos(5)]))
            diagnostics = asyncio.create_task(
                ssh_agent.log_diagnostics(repos, github_test=ssh_agent.github_test_enabled()))
        yield
        if diagnostics:
            diagnostics.cancel()
        await app.state.manager.shutdown()
        db.close()

    app = FastAPI(title="Codex GUI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.auth = AuthService(db, settings.session_hours)
    app.state.manager = TaskManager(settings, db, runner, app_server)
    app.state.catalog = ModelCatalog(settings.codex_bin)

    @app.exception_handler(AuthRequired)
    async def auth_required(request: Request, exc: AuthRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": {"message": "login required", "code": "auth_required"}}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if settings.auth_enabled and request.method not in SAFE_METHODS and not same_origin(request):
            return JSONResponse({"detail": {"message": "cross-origin request refused", "code": "bad_origin"}}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Content-Security-Policy"] = CSP
        if not request.url.path.startswith("/static/"):
            h["Cache-Control"] = "no-store"  # pages and API answers are for one signed-in user only
        if request.url.scheme == "https":
            h["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    app.include_router(auth_routes.public)
    app.include_router(auth_routes.private, dependencies=[Depends(require_login)])
    app.include_router(router, dependencies=[Depends(require_login)])
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
