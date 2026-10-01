import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Make git independent of the developer's global config.
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                  GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
                  GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                  CODEX_GUI_SSH_DIAGNOSTICS="0")  # no ssh-add / ssh -T from tests

from app.appserver import AppServerClient  # noqa: E402
from app.codex_runner import CodexRunner  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.task_manager import TaskManager  # noqa: E402

FAKE = str(Path(__file__).parent / "fake_codex.py")
FAKE_APP_SERVER = str(Path(__file__).parent / "fake_app_server.py")


class FakeRunner(CodexRunner):
    """Runs tests/fake_codex.py instead of codex, through the real spawn/stream/stop code."""

    def build_command(self, task, resume_thread=None):
        return [sys.executable, FAKE] + (["resume", resume_thread] if resume_thread else [])


class FakeAppServer(AppServerClient):
    """The real JSON-RPC client talking to tests/fake_app_server.py instead of `codex app-server`."""

    def build_command(self):
        return [sys.executable, FAKE_APP_SERVER]


@pytest.fixture(autouse=True)
def fake_codex_state(tmp_path_factory, monkeypatch):
    """Where tests/fake_codex.py keeps per-thread usage totals and its invocation log."""
    state = tmp_path_factory.mktemp("fake_state")
    monkeypatch.setenv("FAKE_CODEX_STATE", str(state))
    return state


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    return repo


@pytest.fixture
def settings(tmp_path):
    # The exec backend is what the older tests were written for; app-server tests ask for it via make_manager.
    s = Settings(home=tmp_path / "home", stop_grace_seconds=1.0, git_refresh_seconds=0.2, backend="exec")
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def make_manager(settings, db):
    """make(**settings overrides). backend="app-server" gives the manager a fake app-server process."""
    created = []

    def make(**overrides):
        for k, v in overrides.items():
            setattr(settings, k, v)
        server = FakeAppServer("fake", settings.subscription_only) if settings.backend == "app-server" else None
        manager = TaskManager(settings, db, FakeRunner(), server)
        if server:
            server.on_global(manager._on_global_notification)
        created.append(manager)
        return manager

    yield make
    for m in created:  # a test that failed half way must not leave fake servers behind
        proc = getattr(m._app_server, "_proc", None)
        if proc is not None and proc.returncode is None:
            try:
                os.kill(proc.pid, 9)
            except ProcessLookupError:
                pass


async def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")
