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

from app.codex_runner import CodexRunner  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.task_manager import TaskManager  # noqa: E402

FAKE = str(Path(__file__).parent / "fake_codex.py")


class FakeRunner(CodexRunner):
    """Runs tests/fake_codex.py instead of codex, through the real spawn/stream/stop code."""

    def build_command(self, task):
        return [sys.executable, FAKE]


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
    s = Settings(home=tmp_path / "home", stop_grace_seconds=1.0, git_refresh_seconds=0.2)
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def make_manager(settings, db):
    def make(**overrides):
        for k, v in overrides.items():
            setattr(settings, k, v)
        return TaskManager(settings, db, FakeRunner())
    return make


async def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for condition")
