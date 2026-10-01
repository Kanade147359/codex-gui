"""Test helpers around app.fake_responses: an isolated CODEX_HOME so the real ~/.codex is never touched."""
import os
from pathlib import Path

from app.fake_responses import (  # noqa: F401  (re-exported for the tests)
    MockResponses, tool_names, tool_output_of, tools_of,
)
from app.tool_probe import provider_overrides  # noqa: F401


def codex_home(path: Path, extra_toml: str = "") -> Path:
    """A CODEX_HOME with the real model metadata (truncation policy, multi-agent version, ...) and no auth."""
    path.mkdir(parents=True, exist_ok=True)
    cache = Path("~/.codex/models_cache.json").expanduser()
    if cache.exists():
        (path / "models_cache.json").write_bytes(cache.read_bytes())
    (path / "config.toml").write_text('model = "gpt-6.1-sol"\n' + extra_toml)
    return path


def codex_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    env["CODEX_HOME"] = str(home)
    return env
