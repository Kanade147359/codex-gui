"""Token usage from `codex exec --json` `turn.completed` events, and cache-hit arithmetic.

Verified against codex-cli 0.159.2: `turn.completed.usage` is the running total of the whole
Codex thread (it keeps growing across `codex exec resume`), not the usage of that one turn. The
per-turn figures the GUI shows are therefore the difference to the previous total of the same thread.
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
