"""Explicit role routing. A provider switch always creates an independent session."""
from .execution import validate_settings

ROLES = ("planner", "implementer", "reviewer", "mentor")


def validate_roles(value):
    if not isinstance(value, dict) or set(value) - set(ROLES):
        raise ValueError("unknown agent role")
    result = {}
    for role, config in value.items():
        if not isinstance(config, dict) or set(config) - {"provider", "model", "reasoning_effort", "allow_subagents", "provider_options"}:
            raise ValueError("invalid role settings")
        if config.get("provider", "codex") not in ("codex", "claude"):
            raise ValueError("unknown role provider")
        normalized = {**config, **validate_settings(config, allow_empty_model=True)}
        if "allow_subagents" in config and type(config["allow_subagents"]) is not bool:
            raise ValueError("allow_subagents must be boolean")
        # Isolated readers have no tools; never advertise pseudo subagent support.
        if role != "implementer" and config.get("allow_subagents"):
            raise ValueError("isolated planner/reviewer/mentor roles have no tool/subagent capability")
        if role != "implementer" and config.get("provider_options"):
            raise ValueError("execution permission options apply only to implementers")
        if config.get("provider", "codex") == "claude" and "provider_options" in config:
            from app.providers import ClaudeCodeProvider
            normalized["provider_options"] = ClaudeCodeProvider.validate_options(config["provider_options"])
        result[role] = normalized
    return result


def reader_policy(policy, role, provider):
    config = policy.get("roles", {}).get(role, {})
    result = dict(policy)
    if config.get("model") is not None:
        result[provider + "_model"] = config["model"]
    if config.get("reasoning_effort"):
        result["reasoning_effort"] = config["reasoning_effort"]
    return result
