"""Opt-in real subscription E2E, isolated repo/database; never run in the normal suite.

Set CODEX_GUI_HOOTL_REAL=1, CODEX_GUI_EXTRA_USAGE_DISABLED_CONFIRMED=1 and
CODEX_GUI_OLLAMA_MODEL to an installed local model. This invokes real models.
It is a smoke, not the required 24-hour qualification.
"""

import asyncio
import json
import os

import pytest

from app.task_manager import TaskManager
from app.autopilot.controller import Autopilot
from autopilot_helpers import drive, register

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEX_GUI_HOOTL_REAL") != "1",
    reason="opt-in three-layer real subscription model smoke",
)


def test_real_local_dsh_codex_validation_claude(settings, db, git_repo, tmp_path):
    assert os.environ.get("CODEX_GUI_EXTRA_USAGE_DISABLED_CONFIRMED") == "1", (
        "Owner must confirm both subscriptions have extra usage/credits disabled before real inference"
    )
    model = os.environ.get("CODEX_GUI_OLLAMA_MODEL")
    assert model, "Choose an installed local Ollama model explicitly"
    settings.backend = "app-server"
    settings.subscription_only = True
    manager = TaskManager(settings, db)
    ap = manager.autopilot = Autopilot(manager)
    raw = dict(version=1, name="Three-layer real smoke", repository=str(git_repo), base_ref="main",
               policy=dict(dsh=dict(model=model), review_provider_policy="claude_only",
                           extra_usage_disabled_confirmed=True, mentor_enabled=False, max_fix_rounds=2,
                           reasoning_effort="low"), permissions=dict(commit_owned_changes=True),
               nodes=[dict(id="arithmetic", paths=["arithmetic.py", "test_arithmetic.py"],
                           prompt="Create arithmetic.py with add(a, b) returning a+b. Create stdlib unittest "
                                  "tests in test_arithmetic.py covering positive, negative and zero operands. "
                                  "Run the tests. No dependencies, network, deployment or unrelated edits.",
                           validation=dict(commands=[["python3", "-m", "unittest", "-v", "test_arithmetic"]],
                                           artifacts=["arithmetic.py", "test_arithmetic.py"]))])

    async def scenario():
        identity = None
        try:
            identity = await register(ap, raw)

            def terminal():
                view = ap.ledger.view(identity)
                return view["state"] == "completed" or any(
                    n["state"].startswith("blocked_") or n["state"] in ("waiting_provider", "reconciling")
                    for n in view["nodes"]
                ) or any(e["state"] in ("blocked", "interrupted") for e in view["events"])

            await drive(manager, terminal, timeout=1200)
            view = ap.ledger.view(identity)
            assert view["state"] == "completed", "Real mission did not complete; inspect saved evidence"
            assert view["metrics"]["verified_deliverables"] == 1
            reviews = view["nodes"][0]["candidates"][-1]["reviews"]
            assert reviews[-1]["provider"] == "claude" and reviews[-1]["state"] == "completed"
            assert reviews[-1]["payload"]["session"] != view["nodes"][0]["task"]["codex_thread_id"]
        finally:
            if identity:
                evidence = tmp_path / "hootl-real-evidence.json"
                evidence.write_text(json.dumps(ap.ledger.view(identity), indent=2, ensure_ascii=False))
                print("Real smoke evidence:", evidence)
            await manager.shutdown()

    asyncio.run(scenario())
