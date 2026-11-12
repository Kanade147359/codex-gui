"""Subscription-only CLI adapter regression tests; no model calls."""

import asyncio
import json
from pathlib import Path

import pytest

from app.autopilot.providers import ClaudeProvider, CodexProvider, ProviderFailure, preflight_environment


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                     "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_ACCESS_TOKEN"])
def test_subscription_preflight_forbids_api_and_injected_tokens(monkeypatch, variable):
    monkeypatch.setenv(variable, "test-secret-never-print")
    with pytest.raises(ProviderFailure) as result:
        preflight_environment(dict(extra_usage_disabled_confirmed=True))
    assert result.value.kind == "configuration" and "test-secret" not in str(result.value)


@pytest.mark.parametrize("provider,metadata,ok", [
    ("claude", dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty", subscriptionType="pro"), True),
    ("claude", dict(loggedIn=True, authMethod="api_key", apiProvider="firstParty", subscriptionType="pro"), False),
    ("claude", dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty"), False),
    ("codex", "Logged in using ChatGPT", True),
    ("codex", "Logged in using an API key", False),
])
def test_cli_metadata_without_credential_decoding(monkeypatch, tmp_path, provider, metadata, ok):
    import app.autopilot.providers as adapters
    folder = tmp_path / (".claude" if provider == "claude" else ".codex")
    folder.mkdir()
    credential = folder / (".credentials.json" if provider == "claude" else "auth.json")
    credential.write_text("Not JSON: only the CLI may read this")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(adapters.shutil, "which", lambda binary: "/usr/bin/true")
    monkeypatch.setattr(adapters, "preflight_environment", lambda policy: None)
    commands = []

    async def bounded(argv, **kwargs):
        commands.append(argv)
        if "--version" in argv:
            return 0, "2.1.185" if provider == "claude" else "codex-cli 0.159.2", False
        if "--help" in argv:
            return 0, "--json-schema --tools --setting-sources --strict-mcp-config --safe-mode", False
        assert str(credential) in argv and "--ro-bind" in argv
        return 0, json.dumps(metadata) if isinstance(metadata, dict) else metadata, False

    monkeypatch.setattr(adapters, "run_bounded", bounded)
    adapter = ClaudeProvider() if provider == "claude" else CodexProvider()
    if ok:
        result = asyncio.run(adapter.probe_capabilities_and_auth({"claude_model": ""}))
        assert "auth" in result
    else:
        with pytest.raises(ProviderFailure) as error:
            asyncio.run(adapter.probe_capabilities_and_auth({"claude_model": ""}))
        assert error.value.kind == "auth"
    assert len(commands) >= 2
