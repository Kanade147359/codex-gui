import json

import pytest

from app import ctx_config as cc


def test_presets_are_the_documented_gui_values():
    assert cc.TOOL_OUTPUT_PRESETS == {"default": None, "conservative": 8000, "balanced": 16000, "large": 32000}
    assert cc.SKILLS_PRESETS == {"default": None, "economy": 2000, "balanced": 4000, "large": 8000}


def test_resolve_presets_and_custom_values():
    assert cc.resolve_tool_output("conservative") == ("conservative", 8000)
    assert cc.resolve_tool_output(None) == ("default", None)
    assert cc.resolve_tool_output("balanced", 4000) == ("custom", 4000)
    assert cc.resolve_skills("economy") == ("economy", 2000)
    for bad in (lambda: cc.resolve_tool_output("huge"), lambda: cc.resolve_tool_output(None, 10),
                lambda: cc.resolve_skills(None, 10**9), lambda: cc.resolve_skills("x")):
        with pytest.raises(ValueError):
            bad()


def test_efficiency_config_of_a_new_task():
    task = {"tool_output_limit": 8000, "skills_budget": 2000, "allow_subagents": 0,
            "tool_profile_config": json.dumps(cc.profile_config("minimal", ["a", "b"]))}
    cfg = cc.efficiency_config(task)
    assert cfg["tool_output_token_limit"] == 8000 and cfg["skills.max_context_tokens"] == 2000
    assert cfg["features.multi_agent"] is False and cfg["features.multi_agent_v2.max_concurrent_threads_per_session"] == 1
    assert cfg["agents.max_concurrent_threads_per_session"] == 1
    assert cfg["features.apps"] is False and cfg["mcp_servers.a.enabled"] is False and cfg["mcp_servers.b.enabled"] is False


def test_a_task_that_predates_the_settings_gets_no_overrides():
    assert cc.efficiency_config({}) == {}
    assert cc.efficiency_config({"tool_output_limit": None, "skills_budget": None, "allow_subagents": 1, "tool_profile_config": ""}) == {}


def test_allow_subagents_removes_the_overrides():
    assert not any("multi_agent" in k or k.startswith("agents.") for k in cc.efficiency_config({"allow_subagents": 1}))


def test_broken_profile_json_is_ignored_not_fatal():
    assert cc.efficiency_config({"allow_subagents": 1, "tool_profile_config": "{not json"}) == {}


def test_profiles():
    assert cc.profile_config("full", ["x"]) == {}
    dev = cc.profile_config("development", ["x"])
    assert dev == {"features.apps": False, "features.plugins": False}       # the user's MCP servers stay
    mini = cc.profile_config("minimal", ["x", "y"])
    assert mini == {"features.apps": False, "features.plugins": False, "mcp_servers.x.enabled": False, "mcp_servers.y.enabled": False}
    with pytest.raises(ValueError):
        cc.profile_config("everything")


def test_mcp_server_names_come_from_the_effective_config_only():
    assert cc.mcp_server_names({"mcp_servers": {"b": {}, "a": {}}}) == ["a", "b"]
    assert cc.mcp_server_names({"mcp_servers": {}}) == [] and cc.mcp_server_names(None) == [] and cc.mcp_server_names({"mcp_servers": None}) == []


def test_tool_output_effect_is_honest_about_noops():
    assert cc.tool_output_effect(16000, 10000) == {"effective": False, "cap": 10000, "note": "no effect: this model already caps one tool output at 10,000 tokens"}
    e = cc.tool_output_effect(8000, 10000)
    assert e["effective"] and e["cap"] == 8000 and "lowers" in e["note"]
    assert cc.effective_tool_output_cap(32000, 10000) == 10000 and cc.effective_tool_output_cap(None, 10000) == 10000
    assert cc.effective_tool_output_cap(8000, None) == 8000 and cc.effective_tool_output_cap(None, None) is None


def test_the_gui_never_touches_the_model_window_or_auto_compaction():
    """model_context_window / model_auto_compact_token_limit stay Codex's: no preset, profile or option writes them."""
    from app.codex_runner import task_config
    task = {"model_verbosity": "low", "web_search_enabled": 0, "reasoning_effort": "low", "writable_dirs": "", "feature_flags": "",
            "worktree": "/w", "cwd_subdir": "x", "allow_subagents": 0, "tool_output_limit": 32000, "skills_budget": 8000,
            "tool_profile_config": json.dumps(cc.profile_config("minimal", ["a"]))}
    keys = " ".join(task_config(task))
    assert "model_context_window" not in keys and "auto_compact" not in keys and "compact" not in keys
    import pathlib
    for f in pathlib.Path(__file__).parent.parent.joinpath("app").glob("*.py"):
        text = f.read_text()
        # the names may be mentioned (docs, this very check) but never be set: no `"model_context_window"` key assignment
        assert 'cfg["model_context_window"]' not in text and '"model_auto_compact_token_limit":' not in text
