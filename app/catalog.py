"""Model choices for the New Task form, taken from `codex debug models` (the CLI is the source of truth)."""
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

CACHE_SECONDS = 600
CONFIG_PATH = Path("~/.codex/config.toml")


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
        return {"models": models, "default_model": config_value("model") or "",
                "default_effort": config_value("model_reasoning_effort") or "", "error": error}
