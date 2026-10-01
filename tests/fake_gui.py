"""Runs a TaskManager in a process of its own, the way the GUI does, so that a test can kill it hard ("GUI crash").

usage: fake_gui.py <home> <repo> <prompt>
Starts one task (exec backend, tests/fake_codex.py), waits until it is running with a recorded Codex thread, prints
"READY <task id> <pid>" and then idles. The fake codex child is in its own session, so it survives the kill of this process.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from conftest import FakeRunner  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.task_manager import TaskManager  # noqa: E402


async def main(home: str, repo: str, prompt: str) -> None:
    settings = Settings(home=Path(home), stop_grace_seconds=1.0, git_refresh_seconds=0.2, backend="exec")
    settings.ensure_dirs()
    manager = TaskManager(settings, Database(settings.db_path), FakeRunner())
    task = await manager.create_task(repository=repo, prompt=prompt, name="orphan")
    while True:
        row = manager.get(task["id"])
        if row["status"] == "running" and row["codex_thread_id"]:
            break
        await asyncio.sleep(0.05)
    print("READY", task["id"], row["pid"], flush=True)
    await asyncio.sleep(3600)


asyncio.run(main(*sys.argv[1:4]))
