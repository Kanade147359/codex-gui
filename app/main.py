"""FastAPI app factory. Run with: uvicorn --factory app.main:create_app"""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .codex_runner import CodexRunner
from .config import Settings
from .database import Database
from .routes import router
from .task_manager import TaskManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Optional[Settings] = None, runner: Optional[CodexRunner] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    db = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager.recover()
        yield
        await app.state.manager.shutdown()
        db.close()

    app = FastAPI(title="Codex GUI", lifespan=lifespan)
    app.state.settings = settings
    app.state.manager = TaskManager(settings, db, runner)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
