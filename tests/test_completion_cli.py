"""Installed Codex protocol against a loopback fake model; no subscription/model usage or live state."""
import asyncio
import json
import shutil

import pytest

from app.completion import SCHEMA
from app.codex_runner import CodexRunner
from app.tool_probe import provider_overrides
from conftest import wait_for
from test_ctx_e2e import stack  # noqa: F401 -- isolated CODEX_HOME + loopback Responses fixture

pytestmark = pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is not installed")


@pytest.mark.parametrize("status,outcome", [("SUCCESS", "success"), ("BLOCKED", "blocked"),
                                          ("NEEDS_INPUT", "needs_input"), ("PARTIAL", "incomplete")])
def test_installed_app_server_structured_result(status, outcome, git_repo, stack):
    m, model, create = stack
    model.script = [("text", json.dumps({"status": status, "reason": "Measured final result."}))]
    async def scenario():
        try:
            task = await create(git_repo)
            result = await wait_for(lambda: m.get(task["id"])["status"] == "completed" and m.get(task["id"]))
            assert result["task_outcome"] == outcome, result["outcome_reason"]
            assert json.loads(result["semantic_result"])["status"] == status
            assert model.requests[0]["text"]["format"]["schema"] == SCHEMA
            assert result["retry_count"] == 0
        finally:
            await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,rules,outcome", [
    ("BLOCKED", {}, "blocked"), ("SUCCESS", {"required_paths": ["missing.bin"]}, "incomplete"),
])
def test_installed_exec_exit_zero_does_not_release(status, rules, outcome, git_repo, stack):
    m, model, create = stack
    class LocalRunner(CodexRunner):
        def build_command(self, task, resume_thread=None):
            argv = super().build_command(task, resume_thread)
            argv[2:2] = provider_overrides(model.base_url)
            return argv
    # Only the loopback fake provider is reachable by this test runner; it uses the fixture's isolated CODEX_HOME.
    m.settings.backend = "exec"
    m.runner = LocalRunner("codex", subscription_only=False)
    model.script = [("text", json.dumps({"status": status, "reason": "Measured CLI final result."}))]
    async def scenario():
        try:
            parent = await create(git_repo, completion_contract=rules)
            child = await create(git_repo, depends_on=[parent["id"]])
            result = await wait_for(lambda: m.get(parent["id"])["status"] == "completed" and m.get(parent["id"]))
            assert result["exit_code"] == 0 and result["task_outcome"] == outcome, result["outcome_reason"]
            assert m.get(child["id"])["status"] == "waiting_dependencies" and not m.db.list_attempts(child["id"])
            assert result["retry_count"] == 0 and len(model.requests) == 1
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_installed_app_server_continues_after_needs_input(git_repo, stack):
    m, model, create = stack
    model.script = [("text", json.dumps({"status": "NEEDS_INPUT", "reason": "ABI missing."})),
                    ("text", json.dumps({"status": "SUCCESS", "reason": "ABI supplied."})),
                    ("text", json.dumps({"status": "SUCCESS", "reason": "Dependent completed."}))]
    async def scenario():
        try:
            parent = await create(git_repo)
            child = await create(git_repo, depends_on=[parent["id"]])
            first = await wait_for(lambda: m.get(parent["id"])["status"] == "completed" and m.get(parent["id"]))
            assert first["task_outcome"] == "needs_input" and m.get(child["id"])["status"] == "waiting_dependencies"
            await m.send_instruction(parent["id"], "Use the specified ABI; finish the original request.")
            last = await wait_for(lambda: m.get(parent["id"])["status"] == "completed" and m.get(parent["id"]))
            await wait_for(lambda: m.get(child["id"])["status"] == "completed")
            assert last["task_outcome"] == "success" and last["codex_thread_id"] == first["codex_thread_id"]
            assert len(m.db.list_attempts(child["id"])) == 1 and len(model.requests) == 3
        finally:
            await m.shutdown()
    asyncio.run(scenario())
