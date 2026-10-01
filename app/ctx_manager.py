"""Context Efficiency, the stateful part: thresholds, per-turn recording (cache health, compactions, tool outputs, retry
guard), the task view for the UI, and the live checks (tool-profile verification, prompt preview) that ask the real Codex.

TaskManager keeps only thin hooks into this class so that the efficiency logic lives in one place. Principles:
* nothing here compacts, retries, edits an AGENTS.md or changes a task's Codex settings on its own;
* what the GUI cannot know is shown as unknown, and what it only observes is labelled as observed;
* a setting is only called "optimized" when a measurement against the real Codex showed a reduction.
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from . import agents_audit, cache_health, ctx_config, ctx_guard, tool_probe
from . import git_manager as git
from .cache_health import Thresholds, TurnFacts
from .ctx_config import task_cwd  # noqa: F401  (re-exported)
from .catalog import ModelCatalog
from .config import Settings
from .database import Database
from .models import now_iso
from .turn_observer import TurnObserver

SETTING_PREFIX = "ctx."
VERIFY_TTL = 600.0


def profile_hash(config_json: str) -> str:
    return hashlib.sha1((config_json or "").encode()).hexdigest()[:8]


def tool_profile_id(task: dict) -> str:
    """Name + hash of the frozen tool overrides: it changes exactly when the model's tool definitions would."""
    return f"{task.get('tool_profile') or 'full'}:{profile_hash(task.get('tool_profile_config') or '')}"


def check_subdir(root: str, subdir: str) -> str:
    """A custom sub-directory, relative to the worktree root, that exists and stays inside it. "" = the root itself."""
    sub = (subdir or "").strip().strip("/")
    if not sub or sub == ".":
        return ""
    p = Path(sub)
    if p.is_absolute() or ".." in p.parts or "\x00" in sub or "\\" in sub:
        raise ValueError(f"working directory must be a relative path inside the repository: {subdir}")
    target = (Path(root) / p).resolve()
    try:
        target.relative_to(Path(root).resolve())
    except ValueError:
        raise ValueError(f"working directory leaves the repository: {subdir}") from None
    if not target.is_dir():
        raise ValueError(f"working directory does not exist in the repository: {sub}")
    return str(p)


async def check_subdir_in_ref(repo: str, ref: str, subdir: str) -> str:
    """Like check_subdir, but against the commit the worktree will be made from (the worktree may not exist yet)."""
    sub = (subdir or "").strip().strip("/")
    if not sub or sub == ".":
        return ""
    p = Path(sub)
    if p.is_absolute() or ".." in p.parts or "\x00" in sub or "\\" in sub or sub.startswith("-"):
        raise ValueError(f"working directory must be a relative path inside the repository: {subdir}")
    code, out, _ = await git.run_git(repo, "cat-file", "-t", f"{ref}:{sub}", check=False)
    if code != 0 or out.strip() != "tree":
        raise ValueError(f"working directory does not exist in the repository at the base ref: {sub}")
    return str(p)


class ContextFeatures:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self._thresholds: Optional[Thresholds] = None
        self._catalog = ModelCatalog(settings.codex_bin)
        self._verified: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- thresholds (configurable)

    def thresholds(self) -> Thresholds:
        if self._thresholds is None:
            stored = {}
            for k, v in self.db.get_settings(SETTING_PREFIX).items():
                try:
                    stored[k[len(SETTING_PREFIX):]] = int(v)
                except ValueError:
                    pass
            self._thresholds = cache_health.thresholds_from(stored)
        return self._thresholds

    def threshold_values(self) -> dict:
        t = self.thresholds()
        return {"values": {k: getattr(t, k) for k in cache_health.BOUNDS}, "defaults": {k: getattr(Thresholds(), k) for k in cache_health.BOUNDS},
                "bounds": {k: list(v) for k, v in cache_health.BOUNDS.items()}}

    def set_thresholds(self, values: dict) -> dict:
        """Raises ValueError for an unknown or out-of-range entry; nothing is saved then."""
        clean = cache_health.validate_thresholds(values)
        merged = {**{k: getattr(self.thresholds(), k) for k in cache_health.BOUNDS}, **clean}
        cache_health.validate_thresholds(merged)  # the combination must hold too (warn <= large, hot <= warm)
        for k, v in clean.items():
            self.db.set_setting(SETTING_PREFIX + k, str(v))
        self._thresholds = None
        return self.threshold_values()

    def reset_thresholds(self) -> dict:
        for k in cache_health.BOUNDS:
            self.db.delete_setting(SETTING_PREFIX + k)
        self._thresholds = None
        return self.threshold_values()

    # ---------------------------------------------------------------- creation

    def creation_fields(self, *, tool_output: Optional[str] = None, tool_output_limit: Optional[int] = None,
                        skills: Optional[str] = None, skills_budget: Optional[int] = None, allow_subagents: bool = False,
                        tool_profile: str = "full", mcp_servers: Optional[list] = None, cwd_subdir: str = "",
                        worktree_root: Optional[str] = None) -> dict:
        """The task columns for the context settings, validated. Raises ValueError with a message for the user."""
        out_preset, out_limit = ctx_config.resolve_tool_output(tool_output, tool_output_limit)
        sk_preset, sk_budget = ctx_config.resolve_skills(skills, skills_budget)
        if tool_profile not in ctx_config.TOOL_PROFILES:
            raise ValueError(f"unknown tool profile: {tool_profile} (allowed: {', '.join(ctx_config.TOOL_PROFILES)})")
        profile_cfg = ctx_config.profile_config(tool_profile, mcp_servers)
        return {"tool_output_preset": out_preset, "tool_output_limit": out_limit, "skills_preset": sk_preset, "skills_budget": sk_budget,
                "allow_subagents": int(bool(allow_subagents)), "tool_profile": tool_profile,
                "tool_profile_config": json.dumps(profile_cfg, sort_keys=True) if profile_cfg else "",
                "cwd_subdir": check_subdir(worktree_root, cwd_subdir) if worktree_root else (cwd_subdir or "").strip("/")}

    # ---------------------------------------------------------------- model facts

    async def model_info(self, model: Optional[str]) -> dict:
        """The catalog entry of a model (or the default one): its own tool-output cap, window and multi-agent version."""
        try:
            cat = await self._catalog.get()
        except Exception:  # the catalog is a convenience; a failure must not stop a turn
            return {}
        slug = model or cat.get("default_model") or ""
        return next((m for m in cat.get("models", []) if m["slug"] == slug), {})

    def peek_model(self, model: Optional[str]) -> dict:
        """Like model_info, but only from what is already cached (for synchronous views); {} when unknown."""
        cat = self._catalog._cached or {}
        slug = model or cat.get("default_model") or ""
        return next((m for m in cat.get("models", []) if m["slug"] == slug), {})

    async def preview_skills(self, cwd: str) -> Optional[dict]:
        """The skills catalog under Codex's own default budget (to show what a chosen budget changes)."""
        return tool_probe.skills_catalog_stats(await tool_probe.prompt_input(self.settings.codex_bin, cwd, None))

    # ---------------------------------------------------------------- one turn

    async def observer(self, task: dict, model: Optional[str]) -> TurnObserver:
        info = await self.model_info(model or task.get("model"))
        cap = ctx_config.effective_tool_output_cap(task.get("tool_output_limit"), info.get("tool_output_cap"))
        return TurnObserver(self.thresholds(), cap, task.get("last_request_input"))

    def turn_columns(self, task: dict, turn_tier: Optional[str], prev_row: Optional[dict], started_at: Optional[str],
                     observer: Optional[TurnObserver]) -> dict:
        """The context columns of a new turns row (what was requested for it, and what it did)."""
        cols = {"service_tier": turn_tier or task.get("service_tier") or "default", "verbosity": task.get("model_verbosity"),
                "tool_profile": tool_profile_id(task)}
        gap = cache_health.minutes_between((prev_row or {}).get("finished_at"), started_at)
        if gap is not None:
            cols["idle_before_seconds"] = max(int(gap * 60), 0)
        if observer:
            cols.update(observer.turn_fields())
        return cols

    @staticmethod
    def facts(row: Optional[dict]) -> Optional[TurnFacts]:
        if not row:
            return None
        return TurnFacts(model=row.get("model"), service_tier=row.get("service_tier"), reasoning_effort=row.get("reasoning_effort"),
                         verbosity=row.get("verbosity"), tool_profile=row.get("tool_profile"), thread_id=row.get("thread_id"),
                         kind=row.get("kind") or "turn", started_at=row.get("started_at"), finished_at=row.get("finished_at"),
                         compactions=row.get("compactions") or 0)

    def finish_turn(self, task: dict, row: dict, previous_rows: list[dict], observer: Optional[TurnObserver]) -> dict:
        """Record the events of a stored turn and return the task fields to update (compactions, cache activity, ...)."""
        th, ts, tid, n = self.thresholds(), now_iso(), task["id"], row["turn"]
        fields: dict = {}
        events: list[dict] = list(observer.events) if observer else []
        if observer:
            if observer.cache_activity_at:
                fields["last_cache_activity_at"] = observer.cache_activity_at
            if observer.requests:
                fields["last_request_input"] = observer.requests[-1]["i"]
        # A manual compaction (thread/compact/start) is a turn whose own `contextCompaction` item was counted by the observer too.
        compactions = max(row.get("compactions") or 0, 1) if row["kind"] == "compact" else (row.get("compactions") or 0)
        if compactions:
            fields["compactions"] = (task.get("compactions") or 0) + compactions
            for _ in range(compactions):
                events.append({"kind": "compaction", "severity": "info", "message":
                               "Thread compacted (requested by the user)" if row["kind"] == "compact" else "Codex compacted the thread by itself",
                               "data": {"source": "manual" if row["kind"] == "compact" else "auto"}})
        # cache miss
        if row["kind"] == "turn":
            same = [r for r in previous_rows if r["thread_id"] == row["thread_id"]]
            normal = [r for r in same if r["kind"] == "turn" and r.get("status", "completed") == "completed" and r["input_tokens"]]
            split = cache_health.split_usage(row["input_tokens"], row["cached_input_tokens"], row.get("cache_write_input_tokens"))
            prev = previous_rows[-1] if previous_rows else None
            since = sum(1 for r in previous_rows[-1:] if r["kind"] == "compact")
            miss = cache_health.detect_cache_miss(
                split, [r["cache_hit_rate"] for r in normal if r.get("cache_hit_rate") is not None], th, prev=self.facts(prev),
                cur=self.facts(row), first_of_thread=not same, compactions_since=since)
            if miss:
                events.append({"kind": "cache_miss", "severity": miss["severity"],
                               "message": miss["message"] + ". Possible cause: " + "; ".join(miss["possible_causes"]), "data": miss})
        if fields.get("compactions"):
            stamps = [e["ts"] for e in self.db.list_context_events(tid, ["compaction"], 100)] + [ts] * compactions
            status = cache_health.compaction_status(stamps, th)
            if status["frequent"] and not self._recent_event(tid, "frequent_compaction", th.frequent_compaction_minutes):
                events.append({"kind": "frequent_compaction", "severity": "warning", "message": status["warning"], "data": {"count": status["count"]}})
        for e in events:
            self.db.add_context_event(tid, e["kind"], e["severity"], e["message"], ts, n, e.get("data"))
        return fields

    def _recent_event(self, task_id: str, kind: str, minutes: int) -> bool:
        last = self.db.list_context_events(task_id, [kind], 1)
        gap = cache_health.minutes_between(last[0]["ts"], now_iso()) if last else None
        return gap is not None and gap < minutes

    def record_zone_change(self, task: dict, context_tokens: Optional[int], model: Optional[str], window: Optional[int]) -> None:
        """A `long_context` event when the context first enters a worse zone (never repeated for the same zone)."""
        z = ctx_guard.context_zone(context_tokens, model, window)
        if z["zone"] not in ("warning", "strong", "long"):
            return
        last = self.db.list_context_events(task["id"], ["long_context"], 1)
        if last and last[0]["data"].get("zone") == z["zone"] and last[0]["data"].get("tokens", 0) <= (context_tokens or 0) + 1:
            return
        self.db.add_context_event(task["id"], "long_context", "critical" if z["zone"] == "long" else "warning", z["message"], now_iso(),
                                  None, {"zone": z["zone"], "tokens": context_tokens})

    # ---------------------------------------------------------------- the task view

    def task_view(self, task: dict, turns: list[dict], model: Optional[str], model_cap: Optional[int] = None) -> dict:
        th = self.thresholds()
        limit = task.get("tool_output_limit")
        try:
            check = json.loads(task.get("tool_profile_check") or "") or None
        except ValueError:
            check = None
        zone = ctx_guard.context_zone(task.get("context_tokens"), model, task.get("context_window"))
        if task.get("long_context_ack") == zone["zone"]:
            zone["acknowledged"] = True
        compaction_stamps = [e["ts"] for e in self.db.list_context_events(task["id"], ["compaction"], 200)]
        comp = cache_health.compaction_status(compaction_stamps, th)
        comp["count"] = max(comp["count"], task.get("compactions") or 0)
        events = self.db.list_context_events(task["id"], None, 120)
        stop = task.get("stop_reason") or ""
        return {
            "settings": {
                "tool_output": {"preset": task.get("tool_output_preset") or "default", "limit": limit,
                                "label": ctx_config.TOOL_OUTPUT_LABELS.get(task.get("tool_output_preset") or "default", "Custom"),
                                **ctx_config.tool_output_effect(limit, model_cap)},
                "skills": {"preset": task.get("skills_preset") or "default", "budget": task.get("skills_budget"),
                           "label": ctx_config.SKILLS_LABELS.get(task.get("skills_preset") or "default", "Custom")},
                "allow_subagents": bool(task.get("allow_subagents", 1)),
                "tool_profile": {"name": task.get("tool_profile") or "full", "label": ctx_config.TOOL_PROFILE_LABELS.get(task.get("tool_profile") or "full"),
                                 "overrides": json.loads(task["tool_profile_config"]) if task.get("tool_profile_config") else {},
                                 "check": check, "frozen": True},
                "cwd_subdir": task.get("cwd_subdir") or "", "cwd": task_cwd(task),
            },
            "context_zone": zone,
            "cache_age": cache_health.cache_age(task.get("last_cache_activity_at"), None, th),
            "compaction": comp,
            "events": events,
            "large_tool_outputs": [e for e in events if e["kind"] == "large_tool_output"][-20:],
            "cache_misses": [e for e in events if e["kind"] == "cache_miss"][-20:],
            "stop": ({"reason": stop, "message": ctx_guard.STOP_MESSAGES.get(stop, stop),
                      "blocks_resend": ctx_guard.blocks_resend(stop)} if stop else None),
            "turn_series": [self._turn_point(t) for t in turns if t["kind"] == "turn"][-40:],
        }

    @staticmethod
    def _turn_point(t: dict) -> dict:
        split = cache_health.split_usage(t["input_tokens"], t["cached_input_tokens"], t.get("cache_write_input_tokens"))
        return {"turn": t["turn"], "input": split["input"], "cache_read": split["cache_read"], "cache_write": split["cache_write"],
                "uncached": split["uncached"], "output": t["output_tokens"], "hit_rate": split["hit_rate"],
                "service_tier": t.get("service_tier"), "tool_calls": t.get("tool_calls"), "large_tool_outputs": t.get("large_tool_outputs"),
                "requests": t.get("requests"), "max_request_input": t.get("max_request_input")}

    # ---------------------------------------------------------------- live checks against the real Codex

    async def _baseline(self, cwd: str, force: bool = False) -> dict:
        """Codex's default tool catalog for a directory, measured once per TTL however many tasks ask at the same time."""
        async with self._lock_for("baseline:" + cwd):
            hit = self._verified.get("baseline:" + cwd)
            if hit and not force and time.monotonic() - hit[0] < VERIFY_TTL:
                return hit[1]
            full = await tool_probe.catalog(self.settings.codex_bin, cwd, None)
            if full["ok"]:
                self._verified["baseline:" + cwd] = (time.monotonic(), full)
            return full

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def verify_profile(self, cwd: str, profile: str, mcp_servers: list[str], force: bool = False) -> dict:
        """Measure the tools the model can reach with the profile against Codex's own default. `verified` is True only
        when the profile really reduced the nested tool count (and the byte figures show what the declarations lost).
        Concurrent identical requests share one measurement."""
        overrides = ctx_config.profile_config(profile, mcp_servers)
        key = json.dumps([cwd, profile, sorted(overrides.items())], sort_keys=True, default=str)
        async with self._lock_for(key):
            hit = self._verified.get(key)
            if hit and not force and time.monotonic() - hit[0] < VERIFY_TTL:
                return hit[1]
            full = await self._baseline(cwd, force)
            res = {"profile": profile, "checked_at": now_iso(), "ok": False, "verified": False, "error": "", "overrides": overrides}
            if not full["ok"]:
                res["error"] = full["error"] or "could not measure Codex's default tools"
            elif profile == "full":
                res.update(ok=True, full_count=full["nested_count"], profile_count=full["nested_count"], full_bytes=full["catalog_bytes"],
                           profile_bytes=full["catalog_bytes"], reduced=False, verified=False,
                           note="Full is Codex's default: nothing to verify.")
            else:
                got = await tool_probe.catalog(self.settings.codex_bin, cwd, overrides)
                if not got["ok"]:
                    res["error"] = got["error"] or "could not measure the tools with this profile"
                else:
                    reduced = got["nested_count"] < full["nested_count"]
                    res.update(ok=True, full_count=full["nested_count"], profile_count=got["nested_count"], full_bytes=full["catalog_bytes"],
                               profile_bytes=got["catalog_bytes"], full_groups=full["groups"], profile_groups=got["groups"], reduced=reduced,
                               verified=reduced, tokens_saved_est=max((full["catalog_bytes"] - got["catalog_bytes"]) // 4, 0),
                               note=("" if reduced else "No reduction was measured, so this profile is NOT marked optimized."))
            if res["ok"]:
                self._verified[key] = (time.monotonic(), res)
            return res

    async def preview(self, cwd: str, effective_config: Optional[dict], overrides: Optional[dict] = None) -> dict:
        """What the model will be handed before any task exists: the AGENTS.md chain, the skills catalog, tool-output cap."""
        cfg = effective_config or {}
        audit = agents_audit.audit(
            cwd, max_bytes=cfg.get("project_doc_max_bytes") if isinstance(cfg.get("project_doc_max_bytes"), int) else None,
            markers=cfg.get("project_root_markers") if isinstance(cfg.get("project_root_markers"), list) else None,
            fallbacks=tuple(cfg.get("project_doc_fallback_filenames") or ()))
        messages = await tool_probe.prompt_input(self.settings.codex_bin, cwd, overrides)
        skills = tool_probe.skills_catalog_stats(messages)
        info = await self.model_info(cfg.get("model") if isinstance(cfg.get("model"), str) else None)
        return {"agents": audit, "skills": skills, "mcp_servers": ctx_config.mcp_server_names(cfg),
                "tool_output_cap": info.get("tool_output_cap"), "model": info.get("slug"),
                "multi_agent_version": info.get("multi_agent_version")}
