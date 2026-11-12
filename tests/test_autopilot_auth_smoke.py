"""Opt-in official CLI auth metadata; no credential extraction or model calls."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

from app.autopilot.providers import IsolatedAppServer, authenticated_command
from app.autopilot.runner import sandbox, minimal_env, run_bounded

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEX_GUI_AUTOPILOT_AUTH_SMOKE") != "1",
    reason="opt-in isolated auth metadata smoke; no model calls",
)


def test_claude_isolated_subscription_auth_metadata(tmp_path):
    async def scenario():
        binary = shutil.which("claude")
        credential = Path.home() / ".claude/.credentials.json"
        if not binary or not credential.exists():
            pytest.skip("Claude OAuth CLI unavailable")
        root = tmp_path / "source"
        root.mkdir()
        with tempfile.TemporaryDirectory(prefix="ap-auth-claude-") as scratch:
            argv = authenticated_command(binary, "claude", root, scratch) + [
                "--safe-mode",
                "--setting-sources",
                "",
                "--settings",
                '{"disableAllHooks":true}',
                "auth",
                "status",
                "--json",
            ]
            rc, output, timeout = await run_bounded(argv, env=minimal_env(), timeout=20)
            assert rc == 0 and not timeout, "isolated Claude auth metadata unavailable"
            metadata = json.loads(output)
            assert metadata.get("loggedIn") is True and metadata.get("authMethod") == "claude.ai"
            assert metadata.get("apiProvider") == "firstParty"
            assert metadata.get("subscriptionType") in ("pro", "max")

    asyncio.run(scenario())


def test_codex_isolated_chatgpt_account_metadata_and_fresh_threads(tmp_path):
    async def scenario():
        binary = shutil.which("codex")
        credential = Path.home() / ".codex/auth.json"
        if not binary or not credential.exists():
            pytest.skip("Codex subscription CLI unavailable")
        root = tmp_path / "source"
        root.mkdir()
        with tempfile.TemporaryDirectory(prefix="ap-auth-codex-") as scratch:
            argv = authenticated_command(binary, "codex", root, scratch) + [
                "-c", 'forced_login_method="chatgpt"', "app-server",
            ]
            client = IsolatedAppServer(argv, dict(minimal_env(), CODEX_HOME="/scratch/.codex"))
            try:
                await client.start()
                account = await client.request("account/read", {})
                assert (account.get("account") or {}).get("type") == "chatgpt", "subscription auth not confirmed"
                params = dict(
                    cwd="/scratch",
                    sandbox="read-only",
                    approvalPolicy="never",
                    config={
                        "features": {
                            "shell_tool": False,
                            "shell_snapshot": False,
                            "multi_agent": False,
                            "plugins": False,
                            "goals": False,
                        },
                        "web_search": "disabled",
                        "mcp_servers": {},
                    },
                )
                a = await client.request("thread/start", params)
                b = await client.request("thread/start", params)
                assert a["thread"]["id"] != b["thread"]["id"]
            finally:
                await client.close()

    asyncio.run(scenario())
