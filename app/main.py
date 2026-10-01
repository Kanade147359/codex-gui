"""FastAPI app factory. Run with: uvicorn --factory app.main:create_app"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .appserver import AppServerClient
from .catalog import ModelCatalog
from .codex_runner import CodexRunner
from .config import Settings
from .database import Database
from .routes import router
from . import ssh_agent
from .task_manager import TaskManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Optional[Settings] = None, runner: Optional[CodexRunner] = None,
               app_server: Optional[AppServerClient] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    db = Database(settings.db_path)

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

    app = FastAPI(title="Codex GUI", lifespan=lifespan)
    app.state.settings = settings
    app.state.manager = TaskManager(settings, db, runner, app_server)
    app.state.catalog = ModelCatalog(settings.codex_bin)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
