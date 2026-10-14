"""Opt-in CLI auth metadata only. Private staged credentials; NO model/limit/credit operations."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

from app.autopilot.providers import IsolatedAppServer
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
            folder = Path(scratch) / ".claude"
            folder.mkdir(mode=0o700)
            staged = folder / ".credentials.json"
            staged.write_bytes(credential.read_bytes())
            staged.chmod(0o600)
            argv = sandbox(root, scratch, network=True, cwd="/scratch") + [
                "--ro-bind",
                str(Path(binary).resolve()),
                "/review-cli",
                "--ro-bind",
                str(staged),
                "/scratch/.claude/.credentials.json",
                "--",
                "/review-cli",
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
            assert metadata.get("loggedIn") is True and metadata.get("authMethod") in (
                "oauth_token",
                "claude_ai",
                "claude.ai",
            ), "subscription OAuth not confirmed"

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
            folder = Path(scratch) / ".codex"
            folder.mkdir(mode=0o700)
            staged = folder / "auth.json"
            staged.write_bytes(credential.read_bytes())
            staged.chmod(0o600)
            argv = sandbox(root, scratch, network=True, cwd="/scratch") + [
                "--ro-bind",
                str(Path(binary).resolve()),
                "/review-cli",
                "--ro-bind",
                str(staged),
                "/scratch/.codex/auth.json",
                "--",
                "/review-cli",
                "app-server",
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
