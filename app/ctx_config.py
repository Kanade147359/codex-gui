"""Context-efficiency settings of a task and the Codex config keys they turn into.

Everything here is chosen when the task is created and then frozen: the same keys go into every `thread/start` and
`thread/resume` of the task (a thread whose tool definitions or instructions drift loses its prompt cache). Changing
one later is an explicit, confirmed action (TaskManager.change_tool_profile).

The presets are the GUI's own, not Codex's: they only name a value. What a value really does was measured against
codex-cli 0.159.2 and is described next to it (docs/context-efficiency.md has the numbers).
"""
import json
from pathlib import Path
from typing import Optional

# ---- tool output ----
# `tool_output_token_limit` caps what one tool call returns to the model. Measured with gpt-6.1-sol: it only LOWERS the
# cap. The model's own policy (`truncation_policy.limit`, 10000 tokens) is applied first, so 16000 and 32000 behave
# exactly like Codex's default, and 8000 trims a single huge output by about 4%.
TOOL_OUTPUT_PRESETS = {"default": None, "conservative": 8000, "balanced": 16000, "large": 32000}
TOOL_OUTPUT_LABELS = {"default": "Codex default", "conservative": "Conservative", "balanced": "Balanced", "large": "Large"}
TOOL_OUTPUT_MIN, TOOL_OUTPUT_MAX = 500, 1_000_000

# ---- skills ----
# `skills.max_context_tokens` is the budget of the skills CATALOG (name + description of every skill) in the prompt,
# not of the skill bodies (those are only read when a skill is used).
SKILLS_PRESETS = {"default": None, "economy": 2000, "balanced": 4000, "large": 8000}
SKILLS_LABELS = {"default": "Codex default", "economy": "Economy", "balanced": "Balanced", "large": "Large"}
SKILLS_MIN, SKILLS_MAX = 100, 100_000

# ---- nested agents ----
# `features.multi_agent=false` does NOT remove or disable Codex's sub-agents for models whose catalog entry says
# `multi_agent_version: v2` (gpt-6.1-sol: spawn_agent still succeeds). What does stop them is the v2 concurrency limit:
# with 1 (the root thread itself) every spawn_agent fails with "agent thread limit reached". The tool declarations stay
# in the catalog (cached prefix); only the ability to spawn is gone. The other two keys cover the legacy (v1) path.
SUBAGENTS_OFF = {
    "features.multi_agent": False,
    "features.multi_agent_v2.max_concurrent_threads_per_session": 1,
    "agents.max_concurrent_threads_per_session": 1,
}

# ---- tool profiles ----
# Full: Codex as configured (ChatGPT apps / connectors, plugins and every MCP server of the user).
# Development: no ChatGPT apps and no plugins; the user's own MCP servers stay.
# Minimal: Codex's built-in tools only (shell, patch, image view, goals, clock) -- every MCP server is disabled as well.
# `features.apps=false` was measured to take the nested tool count of a real config from 187 to 8.
TOOL_PROFILES = ("full", "development", "minimal")
TOOL_PROFILE_LABELS = {"full": "Full", "development": "Development", "minimal": "Minimal"}
PROFILE_FEATURES = {"features.apps": False, "features.plugins": False}


def profile_config(profile: str, mcp_servers: Optional[list[str]] = None) -> dict:
    """The Codex overrides of a tool profile. `mcp_servers` are the MCP servers of the user's own config (only Minimal
    turns them off). The result is stored with the task so that it never changes underneath a running thread."""
    if profile not in TOOL_PROFILES:
        raise ValueError(f"unknown tool profile: {profile}")
    if profile == "full":
        return {}
    cfg = dict(PROFILE_FEATURES)
    if profile == "minimal":
        for name in mcp_servers or []:
            cfg[f"mcp_servers.{name}.enabled"] = False
    return cfg


def mcp_server_names(effective_config: Optional[dict]) -> list[str]:
    """MCP server names out of a `config/read` result (the user's own servers, not the built-in codex_apps)."""
    servers = (effective_config or {}).get("mcp_servers")
    return sorted(k for k in servers if isinstance(k, str)) if isinstance(servers, dict) else []


def resolve_preset(value: Optional[str], presets: dict, custom: Optional[int], lo: int, hi: int, what: str) -> tuple[str, Optional[int]]:
    """(preset name, token value or None). `custom` (an explicit number) wins and is reported as "custom"."""
    if custom is not None:
        if not lo <= custom <= hi:
            raise ValueError(f"{what} must be between {lo} and {hi}")
        return "custom", int(custom)
    name = (value or "default").strip().lower()
    if name not in presets:
        raise ValueError(f"unknown {what} preset: {value} (allowed: {', '.join(presets)})")
    return name, presets[name]


def resolve_tool_output(preset: Optional[str], custom: Optional[int] = None) -> tuple[str, Optional[int]]:
    return resolve_preset(preset, TOOL_OUTPUT_PRESETS, custom, TOOL_OUTPUT_MIN, TOOL_OUTPUT_MAX, "tool output limit")


def resolve_skills(preset: Optional[str], custom: Optional[int] = None) -> tuple[str, Optional[int]]:
    return resolve_preset(preset, SKILLS_PRESETS, custom, SKILLS_MIN, SKILLS_MAX, "skills catalog budget")


def efficiency_config(task: dict) -> dict:
    """The context-efficiency part of a task's Codex config. Empty for a task that predates these settings."""
    cfg: dict = {}
    if task.get("tool_output_limit"):
        cfg["tool_output_token_limit"] = int(task["tool_output_limit"])
    if task.get("skills_budget"):
        cfg["skills.max_context_tokens"] = int(task["skills_budget"])
    if "allow_subagents" in task and not task["allow_subagents"]:
        cfg.update(SUBAGENTS_OFF)
    try:
        cfg.update(json.loads(task.get("tool_profile_config") or "{}"))
    except ValueError:
        pass
    return cfg


def effective_tool_output_cap(limit: Optional[int], model_cap: Optional[int]) -> Optional[int]:
    """What cap actually applies: the lower of the task's limit and the model's own policy (None = unknown)."""
    caps = [c for c in (limit, model_cap) if c]
    return min(caps) if caps else None


def tool_output_effect(limit: Optional[int], model_cap: Optional[int]) -> dict:
    """Does the chosen limit change anything for this model? Honest about presets that are no-ops."""
    if not limit:
        return {"effective": False, "cap": model_cap, "note": "Codex default"}
    if model_cap and limit >= model_cap:
        return {"effective": False, "cap": model_cap,
                "note": f"no effect: this model already caps one tool output at {model_cap:,} tokens"}
    return {"effective": True, "cap": limit,
            "note": f"lowers the cap from {model_cap:,} to {limit:,} tokens" if model_cap else f"caps one tool output at {limit:,} tokens"}


def task_cwd(task: dict) -> str:
    """Where Codex runs: the worktree root, or the chosen sub-directory of it (Advanced; relative, validated at creation)."""
    sub = (task.get("cwd_subdir") or "").strip("/")
    return str(Path(task["worktree"]) / sub) if sub else task["worktree"]
