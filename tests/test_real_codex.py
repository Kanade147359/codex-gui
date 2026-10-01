"""Opt-in check against the real codex CLI: CODEX_GUI_REAL=1 pytest tests/test_real_codex.py -s

It spends a few tokens (CODEX_GUI_REAL_PAUSE=<seconds> waits between turns). Same thread across turns is the REQUIRED outcome; cached input is only printed,
because prompt caching is best-effort and must not make the test flaky.
"""
import asyncio
import os

import pytest

from app.codex_runner import CodexRunner
from app.task_manager import TaskManager

from conftest import wait_for

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_REAL") != "1", reason="set CODEX_GUI_REAL=1 to use the real codex")

PROMPTS = [
    "Reply with exactly the word: one. Do not run any commands.",
    "Reply with exactly the word: two. Do not run any commands.",
    "Reply with exactly the word: three. Do not run any commands.",
]


def test_three_turns_resume_one_real_session(git_repo, settings, db):
    m = TaskManager(settings, db, CodexRunner(os.environ.get("CODEX_BIN", "codex")))

    async def scenario():
        t = await m.create_task(repository=str(git_repo), prompt=PROMPTS[0], name="real", auto_approval=True)

        def idle(n):
            row = m.get(t["id"])
            return row if row["status"] not in ("queued", "starting", "running") and len(m.db.list_turns(t["id"])) >= n else None

        row = await wait_for(lambda: idle(1), timeout=240)
        thread = row["codex_thread_id"]
        assert row["status"] == "completed" and thread
        for n, prompt in enumerate(PROMPTS[1:], start=2):
            # The cache needs a moment to become usable; CODEX_GUI_REAL_PAUSE=<seconds> spaces the turns out.
            await asyncio.sleep(float(os.environ.get("CODEX_GUI_REAL_PAUSE", "0")))
            await m.send_instruction(t["id"], prompt)
            row = await wait_for(lambda: idle(n), timeout=240)
            assert row["status"] == "completed"
            assert row["codex_thread_id"] == thread

        turns = m.usage(t["id"])["turns"]
        print(f"\nthread {thread}\nTURN  INPUT  CACHED  UNCACHED  HIT  OUTPUT")
        for r in turns:
            hit = "-" if r["cache_hit_rate"] is None else f"{r['cache_hit_rate']:.1f}%"
            print(f"{r['turn']:>4} {r['input_tokens']:>6} {r['cached_input_tokens']:>7} {r['uncached_input_tokens']:>9} {hit:>5} {r['output_tokens']:>7}")
        assert [r["thread_id"] for r in turns] == [thread] * 3  # every turn reported the same session
        assert [r["session"] for r in turns] == [1, 1, 1]
        log = [e["message"] for e in m.read_log(t["id"])[0] if e["stream"] == "system"]
        assert not any(msg.startswith("WARNING") for msg in log)

    asyncio.run(scenario())
