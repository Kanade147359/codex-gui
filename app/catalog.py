"""Model choices for the New Task form, taken from `codex debug models` (the CLI is the source of truth)."""
import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

CACHE_SECONDS = 600
CONFIG_PATH = Path("~/.codex/config.toml")
# The GUI's recommended model. It is used only if this codex lists it (`codex debug models`); otherwise the New Task
# form falls back to "Codex default". CODEX_GUI_PREFERRED_MODEL overrides it, an empty value turns the preference off.
PREFERRED_MODEL = "gpt-6.1-sol"


def tool_output_cap(model: dict) -> Optional[int]:
    policy = model.get("truncation_policy")
    if isinstance(policy, dict) and policy.get("mode") == "tokens" and isinstance(policy.get("limit"), int):
        return policy["limit"]
    return None


def parse_catalog(data: dict) -> list[dict]:
    """Keep user-selectable models, best first. Each: slug, name, default_effort, efforts."""
    models = []
    for m in data.get("models", []):
        if m.get("visibility") != "list" or not m.get("slug"):
            continue
        efforts = [e["effort"] for e in m.get("supported_reasoning_levels", []) if isinstance(e, dict) and e.get("effort")]
        models.append({
            "slug": m["slug"],
            "name": m.get("display_name") or m["slug"],
            "default_effort": m.get("default_reasoning_level") or "",
            "efforts": efforts,
            "priority": m.get("priority", 999),
            # Fast / priority tiers the model offers (Standard = "default" is always possible).
            "service_tiers": [{"id": t["id"], "name": t.get("name") or t["id"], "description": t.get("description") or ""}
                              for t in m.get("service_tiers", []) if isinstance(t, dict) and t.get("id")],
            "supports_verbosity": bool(m.get("support_verbosity")),
            "default_verbosity": m.get("default_verbosity") or "",
            "context_window": m.get("context_window") if isinstance(m.get("context_window"), int) else None,
            # The model's own cap on one tool output (tokens): tool_output_token_limit can only lower it.
            "tool_output_cap": tool_output_cap(m),
            "multi_agent_version": m.get("multi_agent_version"),
        })
    models.sort(key=lambda m: m["priority"])
    return models


def config_value(key: str, config_path: Path = CONFIG_PATH) -> Optional[str]:
    """Top-level `key = "value"` from ~/.codex/config.toml (enough for model / model_reasoning_effort)."""
    try:
        text = config_path.expanduser().read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            return None  # reached the first table: top-level keys are over
        m = re.match(rf'\s*{re.escape(key)}\s*=\s*"([^"]*)"', line)
        if m:
            return m.group(1)
    return None


class ModelCatalog:
    def __init__(self, codex_bin: str):
        self.codex_bin = codex_bin
        self._cached: Optional[dict] = None
        self._fetched_at = 0.0

    async def get(self) -> dict:
        if self._cached is None or time.monotonic() - self._fetched_at > CACHE_SECONDS:
            self._cached = await self._load()
            self._fetched_at = time.monotonic()
        return self._cached

    async def _load(self) -> dict:
        models, error = [], ""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.codex_bin, "debug", "models",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
            models = parse_catalog(json.loads(out))
        except (OSError, ValueError, asyncio.TimeoutError) as e:
            error = f"could not read model list from codex: {e}"
        preferred = os.environ.get("CODEX_GUI_PREFERRED_MODEL", PREFERRED_MODEL)
        return {"models": models, "default_model": config_value("model") or "",
                "default_effort": config_value("model_reasoning_effort") or "",
                "recommended_model": preferred if any(m["slug"] == preferred for m in models) else "",
                "error": error}
