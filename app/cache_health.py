"""Cache Health: per-request/turn cache accounting, cache-miss detection with possible causes, cache age, and the
compaction monitor. Pure functions over numbers the GUI already stores; nothing here talks to Codex.

Wording rule: a cause is only ever a *possible* cause. The GUI knows what it changed (model, speed tier, reasoning
effort, verbosity, tool profile, compaction, idle time); it cannot see inside Codex's prompt, so it never asserts.
Cache age is a reference label. The GUI NEVER sends a prompt just to keep a cache warm.
"""
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Optional

FREQUENT_COMPACTION_WARNING = "Frequent compaction can reduce cache reuse and cause files to be re-read"


@dataclass
class Thresholds:
    """Configurable limits (stored in the DB, editable in the UI). Every field has a sane lower and upper bound."""
    cache_miss_uncached_tokens: int = 10_000    # a turn with more uncached input than this is a cache miss ...
    cache_miss_drop_points: int = 30            # ... or a hit rate this many points below the recent average
    cache_miss_min_input: int = 4_000           # turns smaller than this are never judged by the hit rate
    cache_recent_turns: int = 5                 # how many earlier turns make "the recent average"
    tool_output_warn_tokens: int = 8_000        # a tool output above this is listed in Task Detail
    tool_output_large_tokens: int = 16_000      # ... and marked large above this
    context_jump_tokens: int = 8_000            # input growth after a large tool output that raises a warning
    frequent_compaction_count: int = 2          # compactions within the window below that count as "frequent"
    frequent_compaction_minutes: int = 60
    cache_hot_minutes: int = 10                 # cache age: HOT up to here, WARM up to cache_warm_minutes, then COLD
    cache_warm_minutes: int = 30                # Codex keeps the prompt cache for at least 30 minutes
    idle_cause_minutes: int = 30                # an idle gap this long is listed as a possible cause of a miss
    repeat_failure_limit: int = 3               # identical failing tool calls in a row before the turn is stopped


BOUNDS = {
    "cache_miss_uncached_tokens": (500, 5_000_000), "cache_miss_drop_points": (5, 100), "cache_miss_min_input": (0, 5_000_000),
    "cache_recent_turns": (1, 50), "tool_output_warn_tokens": (500, 5_000_000), "tool_output_large_tokens": (500, 5_000_000),
    "context_jump_tokens": (500, 5_000_000), "frequent_compaction_count": (2, 50), "frequent_compaction_minutes": (1, 10_080),
    "cache_hot_minutes": (1, 10_080), "cache_warm_minutes": (1, 10_080), "idle_cause_minutes": (1, 10_080),
    "repeat_failure_limit": (2, 20),
}


def thresholds_from(values: Optional[dict]) -> Thresholds:
    """Thresholds with the stored overrides applied; unknown keys and out-of-range values are ignored."""
    t = Thresholds()
    for k, v in (values or {}).items():
        if k in BOUNDS and isinstance(v, int) and not isinstance(v, bool) and BOUNDS[k][0] <= v <= BOUNDS[k][1]:
            setattr(t, k, v)
    return t


def validate_thresholds(values: dict) -> dict:
    """Strict version for the API: raises ValueError naming the first bad entry."""
    out = {}
    for k, v in values.items():
        if k not in BOUNDS:
            raise ValueError(f"unknown threshold: {k}")
        lo, hi = BOUNDS[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"{k} must be an integer between {lo:,} and {hi:,}")
        out[k] = v
    merged = {**{f.name: getattr(Thresholds(), f.name) for f in fields(Thresholds)}, **out}
    if merged["tool_output_large_tokens"] < merged["tool_output_warn_tokens"]:
        raise ValueError("tool_output_large_tokens must not be below tool_output_warn_tokens")
    if merged["cache_warm_minutes"] < merged["cache_hot_minutes"]:
        raise ValueError("cache_warm_minutes must not be below cache_hot_minutes")
    return out


def parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def minutes_between(a, b) -> Optional[float]:
    da, db = parse_ts(a), parse_ts(b)
    return (db - da).total_seconds() / 60 if da and db else None


# ---------------------------------------------------------------- per-request accounting

def split_usage(input_tokens: int, cached: int, write: Optional[int]) -> dict:
    """Input split into cache read / cache write / uncached. `input` is assumed to contain both the cached and the
    written tokens (the Responses API reports them as parts of input_tokens); if the parts exceed the input they are
    clamped and `anomaly` is set rather than silently producing a negative number."""
    inp = max(int(input_tokens or 0), 0)
    read = max(int(cached or 0), 0)
    wr = max(int(write or 0), 0)
    anomaly = read + wr > inp
    read = min(read, inp)
    wr = min(wr, inp - read)
    return {"input": inp, "cache_read": read, "cache_write": wr, "uncached": inp - read,
            "uncached_regular": inp - read - wr, "hit_rate": read / inp * 100 if inp else None, "anomaly": anomaly}


# ---------------------------------------------------------------- cache miss detection

@dataclass
class TurnFacts:
    """What the GUI knows about one turn, for comparing it with the one before. Every field may be None (unknown)."""
    model: Optional[str] = None
    service_tier: Optional[str] = None
    reasoning_effort: Optional[str] = None
    verbosity: Optional[str] = None
    tool_profile: Optional[str] = None      # a name plus a hash of the frozen overrides, so any change shows
    thread_id: Optional[str] = None
    kind: str = "turn"                      # "turn" | "compact"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    compactions: int = 0                    # compactions Codex ran during the turn (auto) or this turn being one (manual)


_CHANGE_LABELS = [("model", "model change"), ("service_tier", "service tier change (Standard/Fast)"),
                  ("reasoning_effort", "reasoning effort change"), ("verbosity", "verbosity change"),
                  ("tool_profile", "tool profile / configuration change")]


def possible_causes(prev: Optional[TurnFacts], cur: TurnFacts, th: Thresholds, previous_turns_between_compaction: int = 0) -> list[str]:
    """Things the GUI knows changed since the previous turn of the same task. Possible causes only, never a verdict."""
    causes = []
    if prev is None:
        return ["first turn of the task: nothing was cached yet"]
    if prev.thread_id and cur.thread_id and prev.thread_id != cur.thread_id:
        causes.append("new Codex session: the earlier thread's cache is not reused")
    for attr, label in _CHANGE_LABELS:
        a, b = getattr(prev, attr), getattr(cur, attr)
        if a is not None and b is not None and a != b:
            causes.append(f"{label} ({a} -> {b})")
    if prev.kind == "compact" or prev.compactions or cur.compactions or previous_turns_between_compaction:
        causes.append("compaction (the compacted history is a new prompt prefix)")
    gap = minutes_between(prev.finished_at, cur.started_at)
    if gap is not None and gap >= th.idle_cause_minutes:
        causes.append(f"idle for {gap:.0f} minutes before this turn (the cache may have expired)")
    return causes


def detect_cache_miss(split: dict, recent_hit_rates: list[float], th: Thresholds, *, prev: Optional[TurnFacts],
                      cur: TurnFacts, first_of_thread: bool = False, compactions_since: int = 0) -> Optional[dict]:
    """A cache-miss event for a turn, or None. `recent_hit_rates` are the hit rates (%) of the earlier normal turns of
    the same thread, newest last. The first turn of a thread cannot hit a cache, so it only counts when it is big."""
    if cur.kind != "turn" or not split["input"]:
        return None
    uncached, rate = split["uncached"], split["hit_rate"]
    recent = [r for r in recent_hit_rates[-th.cache_recent_turns:] if r is not None]
    avg = sum(recent) / len(recent) if recent else None
    reasons = []
    if uncached > th.cache_miss_uncached_tokens:
        reasons.append(f"{uncached:,} uncached input tokens (limit {th.cache_miss_uncached_tokens:,})")
    if (avg is not None and rate is not None and split["input"] >= th.cache_miss_min_input
            and rate <= avg - th.cache_miss_drop_points):
        reasons.append(f"cache hit {rate:.0f}% vs recent average {avg:.0f}% (down {avg - rate:.0f} points)")
    if not reasons:
        return None
    causes = possible_causes(None if first_of_thread else prev, cur, th, compactions_since)
    unknown = not causes
    return {
        "kind": "cache_miss", "severity": "info" if first_of_thread else "warning",
        "message": "Large cache miss: " + "; ".join(reasons),
        "uncached_tokens": uncached, "hit_rate": rate, "recent_average": avg,
        "possible_causes": causes or ["no change the GUI knows of: the prompt prefix may have changed inside Codex "
                                      "(instructions, AGENTS.md, earlier tool output) or the cache was evicted"],
        "cause_unknown": unknown, "first_of_thread": first_of_thread,
    }


# ---------------------------------------------------------------- cache age

def cache_age(last_activity: Optional[str], now: Optional[datetime] = None, th: Optional[Thresholds] = None) -> dict:
    """HOT / WARM / COLD from the time of the last successful cache write or reuse. A reference, not a guarantee:
    the cache is kept for at least 30 minutes. `state` is "unknown" when no cache activity was ever seen."""
    th = th or Thresholds()
    last = parse_ts(last_activity)
    if last is None:
        return {"state": "unknown", "minutes": None, "label": "no cache activity yet", "last_activity": None}
    now = now or datetime.now(timezone.utc)
    minutes = max((now - last).total_seconds() / 60, 0.0)
    state = "hot" if minutes < th.cache_hot_minutes else "warm" if minutes < th.cache_warm_minutes else "cold"
    return {"state": state, "minutes": round(minutes, 1), "last_activity": last_activity,
            "label": f"{state.upper()} ({minutes:.0f} min since the last cache write/reuse)"}


# ---------------------------------------------------------------- compaction monitor

def compaction_status(timestamps: list[str], th: Optional[Thresholds] = None, now: Optional[datetime] = None) -> dict:
    """Count and the "frequent compaction" verdict: `frequent_compaction_count` or more compactions inside any window of
    `frequent_compaction_minutes`. Never triggers a compaction; it only describes."""
    th = th or Thresholds()
    times = sorted(t for t in (parse_ts(x) for x in timestamps) if t)
    window = th.frequent_compaction_minutes * 60
    frequent = False
    for i, t in enumerate(times):
        inside = [u for u in times[i:] if (u - t).total_seconds() <= window]
        if len(inside) >= th.frequent_compaction_count:
            frequent = True
            break
    return {"count": len(timestamps), "frequent": frequent, "warning": FREQUENT_COMPACTION_WARNING if frequent else None,
            "window_minutes": th.frequent_compaction_minutes}
