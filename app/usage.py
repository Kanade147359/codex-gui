"""Token usage, cache-hit arithmetic, context size and rate-limit snapshots.

Verified against codex-cli 0.159.2. Both `codex exec --json` (`turn.completed.usage`) and the app-server
(`thread/tokenUsage/updated` -> `tokenUsage.total`) report the running total of the whole Codex thread, which
keeps growing across resumes. The per-turn figures the GUI shows are therefore the difference to the previous
total of the same thread. The app-server also reports `tokenUsage.last` (the most recent model request, i.e. the
current context size) and `modelContextWindow`.
"""
import json
from typing import Optional

# Reported by the CLI today. The last two are optional: stored as NULL when the CLI omits them.
REQUIRED_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")
OPTIONAL_KEYS = ("cache_write_input_tokens", "reasoning_output_tokens")
USAGE_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS


def extract_usage(event: Optional[dict]) -> Optional[dict]:
    """The usage dict of a turn.completed event (ints only, unknown keys dropped), else None."""
    if not isinstance(event, dict) or event.get("type") != "turn.completed":
        return None
    raw = event.get("usage")
    if not isinstance(raw, dict):
        return None
    usage = {k: raw[k] for k in USAGE_KEYS if isinstance(raw.get(k), int) and not isinstance(raw.get(k), bool)}
    return usage or None


def turn_delta(total: dict, previous_total: Optional[dict]) -> tuple[dict, bool]:
    """(usage of this turn, total_was_not_cumulative).

    `previous_total` is the last reported total of the same thread (None for its first turn).
    If any figure went down the CLI evidently reported a per-turn value, so `total` is used as is.
    """
    if not previous_total:
        return dict(total), False
    delta = {k: v - previous_total.get(k, 0) for k, v in total.items()}
    if any(v < 0 for v in delta.values()):
        return dict(total), True
    return delta, False


def cache_hit_rate(input_tokens: Optional[int], cached_input_tokens: Optional[int]) -> Optional[float]:
    """cached / input * 100, or None when there is no input to divide by (nothing to display)."""
    if not input_tokens or cached_input_tokens is None:
        return None
    return cached_input_tokens / input_tokens * 100


def dumps(usage: dict) -> str:
    return json.dumps(usage, sort_keys=True)


# ---------- app-server token usage ----------

# app-server spelling -> the names used in the DB and the exec events
_APP_SERVER_KEYS = {
    "inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens", "outputTokens": "output_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens", "reasoningOutputTokens": "reasoning_output_tokens",
}


def _ints(raw: Optional[dict]) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {new: raw[old] for old, new in _APP_SERVER_KEYS.items()
            if isinstance(raw.get(old), int) and not isinstance(raw.get(old), bool)}


def parse_token_usage(token_usage: Optional[dict]) -> Optional[dict]:
    """A `thread/tokenUsage/updated` payload -> {"total": {...}, "context_tokens", "context_window"}.

    `total` uses the DB names and only holds the figures Codex actually sent. `context_tokens` is the size of the
    latest model request (`last.totalTokens`); both context fields are None when Codex did not report them.
    """
    if not isinstance(token_usage, dict):
        return None
    total = _ints(token_usage.get("total"))
    if not total:
        return None
    last = token_usage.get("last") if isinstance(token_usage.get("last"), dict) else {}
    ctx = last.get("totalTokens")
    window = token_usage.get("modelContextWindow")
    ok = lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0  # noqa: E731
    return {"total": total, "context_tokens": ctx if ok(ctx) else None, "context_window": window if ok(window) else None}


def context_status(tokens: Optional[int], window: Optional[int], warn_percent: int = 80, guard: bool = True) -> dict:
    """Context size for the UI. `percent` is None unless both numbers are known; `warn` is the Context Guard verdict."""
    percent = tokens / window * 100 if tokens is not None and window else None
    return {"tokens": tokens, "window": window, "percent": percent,
            "warn": bool(guard and percent is not None and percent >= warn_percent)}


# ---------- quota ----------

# codexErrorInfo values that mean "the included usage is used up" (CodexErrorInfo in the app-server schema).
QUOTA_ERRORS = frozenset({"usageLimitExceeded", "rateLimitExceeded"})
# GetAccountRateLimitsResponse.rateLimits[].rateLimitReachedType: any non-null value means a limit was hit.


def is_quota_error(error_info) -> bool:
    return isinstance(error_info, str) and error_info in QUOTA_ERRORS


FIVE_HOUR_MAX_MINS = 360          # a window up to 6 h is "5 hour"
WEEKLY_MIN_MINS = 6 * 24 * 60     # a window of 6 days or more is "weekly"


def window_kind(duration_mins: Optional[int]) -> str:
    """"five_hour" | "weekly" | "other", from the window length Codex reports (never from which slot it came in)."""
    if not isinstance(duration_mins, int):
        return "other"
    if duration_mins <= FIVE_HOUR_MAX_MINS:
        return "five_hour"
    return "weekly" if duration_mins >= WEEKLY_MIN_MINS else "other"


def window_label(duration_mins: Optional[int]) -> str:
    kind = window_kind(duration_mins)
    if kind == "five_hour":
        return "5 hour"
    if kind == "weekly":
        return "Weekly"
    if isinstance(duration_mins, int) and duration_mins > 0:
        hours = duration_mins / 60
        return f"{hours:g} hour" if hours < 48 else f"{hours / 24:g} day"
    return "Usage"


def _window(raw) -> Optional[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("usedPercent"), (int, float)):
        return None
    mins = raw.get("windowDurationMins") if isinstance(raw.get("windowDurationMins"), int) else None
    return {"kind": window_kind(mins), "label": window_label(mins), "used_percent": raw["usedPercent"],
            "duration_mins": mins, "resets_at": raw.get("resetsAt") if isinstance(raw.get("resetsAt"), int) else None}


def parse_rate_limits(result: Optional[dict]) -> Optional[dict]:
    """`account/rateLimits/read` result (or the `rateLimits` of an update, via `snapshot_only`) -> display model.

    Windows are classified by their duration: this account's plan reports a single 10080-minute window, other plans
    report 300 + 10080, so a fixed "primary = 5 hour" mapping would be wrong. `five_hour_used` / `weekly_used` are
    None when no window of that kind exists.
    """
    if not isinstance(result, dict):
        return None
    snap = result.get("rateLimits")
    if not isinstance(snap, dict):
        return None
    windows = [w for w in (_window(snap.get("primary")), _window(snap.get("secondary"))) if w]
    by_kind = {}
    for w in windows:
        by_kind.setdefault(w["kind"], w)
    credits = result.get("rateLimitResetCredits") if isinstance(result.get("rateLimitResetCredits"), dict) else {}
    allowed = result.get("ordinaryUsageAllowed")
    return {
        "windows": windows,
        "five_hour_used": by_kind["five_hour"]["used_percent"] if "five_hour" in by_kind else None,
        "weekly_used": by_kind["weekly"]["used_percent"] if "weekly" in by_kind else None,
        "ordinary_usage_allowed": allowed if isinstance(allowed, bool) else None,  # None: unknown, never "allowed"
        "reached_type": snap.get("rateLimitReachedType") if isinstance(snap.get("rateLimitReachedType"), str) else None,
        "plan_type": snap.get("planType") if isinstance(snap.get("planType"), str) else None,
        "limit_id": snap.get("limitId") if isinstance(snap.get("limitId"), str) else None,
        # Shown only. The GUI never redeems a reset (account/rateLimitResetCredit/consume is not used anywhere).
        "available_resets": credits.get("availableCount") if isinstance(credits.get("availableCount"), int) else None,
    }


def quota_exhausted(limits: Optional[dict]) -> bool:
    """True only when Codex says so explicitly. Unknown (None) is never treated as exhausted."""
    return bool(limits) and (limits.get("ordinary_usage_allowed") is False or limits.get("reached_type") is not None)
