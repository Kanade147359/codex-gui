"""Efficiency / Savings: what the prompt cache and the model choice save, in credit- and API-*equivalent* terms.

These are estimates computed from token counts and a published rate card. They are NOT the amount of the ChatGPT
subscription that was saved. Two comparisons are made per turn:

* the same Sol model without any cache (what the persistent session + prompt cache saves), and
* GPT-6 Astra at Standard speed with the same token counts (a hypothetical "same-token estimate").

Two different "Fast" multipliers exist and must not be mixed up: the credit / USD price of Fast is Standard x 2
(`PRICE_MULTIPLIER`, folded into the Fast rows of PRICING), while the included subscription allowance is consumed
2.5x as fast as Standard (`ALLOWANCE_MULTIPLIER_FAST`, display only, never used in any cost below).
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

M = 1_000_000
PRICING_VERSION = "2026-10-01"
SOURCE = "Codex rate card (credits) and OpenAI API pricing (USD), Standard rates as of 2026-10-01"

SPEED_STANDARD, SPEED_FAST = "standard", "fast"
# Codex's own service-tier ids -> pricing speed. Anything else has no price here and is never guessed.
SPEED_BY_TIER = {"default": SPEED_STANDARD, "priority": SPEED_FAST}
PRICE_MULTIPLIER = {SPEED_STANDARD: 1, SPEED_FAST: 2}   # credits and USD
ALLOWANCE_MULTIPLIER_FAST = 2.5                         # included subscription allowance only; not a price

SOL, ASTRA = "gpt-6.1-sol", "gpt-6-astra"
# Standard rates per 1M tokens: (input, cached_input, output). Cache write is priced separately where the rate is known.
_STANDARD = {
    SOL: {"credits": (50, 2.5, 250), "usd": (2.00, 0.10, 10.00)},
    ASTRA: {"credits": (250, 25, 1250), "usd": (10.00, 1.00, 50.00)},
}
# Cache-write rate per 1M tokens (Standard). GPT-6.1 Sol: $2.50 (official pricing page, 2026-10-01; Fast = x2 like every rate).
# Where there is none (credits, Astra) the written tokens are priced as ordinary uncached input: nothing is invented.
_CACHE_WRITE = {SOL: {"usd": 2.50}}
# Long-context pricing (GPT-6.1 Sol, official pricing page 2026-10-01): a request whose INPUT exceeds the threshold is priced
# at input/cached/write x2 and output x1.5 (USD $4.00 / $0.20 / $5.00 / $15.00 per 1M at Standard). It is applied per
# request, and only when Codex reported that request's input size (tokenUsage.last); never to a thread's accumulated usage.
# Credits are assumed to scale like USD (every credit rate is 25 x the USD rate).
LONG_CONTEXT = {SOL: {"threshold": 272_000, "input": 2.0, "cached_input": 2.0, "cache_write": 2.0, "output": 1.5}}


def long_context_threshold(model: Optional[str]) -> Optional[int]:
    entry = LONG_CONTEXT.get((model or "").strip().lower())
    return entry["threshold"] if entry else None


def _row(model: str, speed: str, unit: str, rates: tuple) -> dict:
    k = PRICE_MULTIPLIER[speed]
    write = _CACHE_WRITE.get(model, {}).get(unit)
    return {"effective_date": PRICING_VERSION, "source": SOURCE, "model": model, "speed": speed, "unit": unit,
            "input": rates[0] * k, "cached_input": rates[1] * k, "output": rates[2] * k,
            "cache_write": write * k if write is not None else None}


# The single place to update when rates change: one row per (model, speed, unit).
PRICING = [_row(m, s, unit, rates[unit]) for m, rates in _STANDARD.items() for s in (SPEED_STANDARD, SPEED_FAST)
           for unit in ("credits", "usd")]
_INDEX = {(r["model"], r["speed"], r["unit"]): r for r in PRICING}

# The model Sol is compared with; always Standard speed.
COMPARE_MODEL = ASTRA

NOTES = [
    "Equivalent estimate only, not the ChatGPT subscription amount actually saved.",
    "Cache write is priced separately where a rate is known (GPT-6.1 Sol in USD); elsewhere written tokens count as uncached input.",
]
ASTRA_NOTE = "Astra figures are a same-token estimate: Astra would not necessarily use the same token counts."
AUTO_REVIEW_NOTE = "Auto-review usage may not be included."
UNPRICED = "pricing unavailable"


def rate(model: Optional[str], tier: Optional[str], unit: str) -> Optional[dict]:
    """The pricing row for a model + Codex service tier, or None (unknown model or tier: never guessed)."""
    speed = SPEED_BY_TIER.get((tier or "default").strip().lower() or "default")
    if speed is None or not model:
        return None
    return _INDEX.get((model.strip().lower(), speed, unit))


def _lc(model: str, long_context: bool) -> dict:
    entry = LONG_CONTEXT.get(model) if long_context else None
    return entry or {"input": 1.0, "cached_input": 1.0, "cache_write": 1.0, "output": 1.0}


def _request_cost(r: dict, inp: int, cached: int, write: int, output: int, long_context: bool = False) -> float:
    """One model request: uncached (regular) input, cache read, cache write and output, each at its own rate."""
    m = _lc(r["model"], long_context)
    wr = r["cache_write"] if r["cache_write"] is not None else r["input"]
    regular = inp - cached - write
    return (regular * r["input"] * m["input"] + cached * r["cached_input"] * m["cached_input"]
            + write * wr * m["cache_write"] + output * r["output"] * m["output"]) / M


def _no_cache_cost(r: dict, inp: int, output: int, long_context: bool = False) -> float:
    m = _lc(r["model"], long_context)
    return (inp * r["input"] * m["input"] + output * r["output"] * m["output"]) / M


def _unit_figures(sol: dict, astra: dict, requests: list[tuple]) -> dict:
    """requests: (input, cached, cache_write, output, long_context) per model request."""
    actual = no_cache = astra_cost = actual_in = no_cache_in = write_cost = 0.0
    for inp, cached, write, out, lc in requests:
        actual += _request_cost(sol, inp, cached, write, out, lc)
        no_cache += _no_cache_cost(sol, inp, out, lc)
        astra_cost += _request_cost(astra, inp, cached, write, out)  # Astra: no long-context rate known, not modelled
        # Input side only (output excluded) for the effective input multiplier; cached input is priced, never free.
        actual_in += _request_cost(sol, inp, cached, write, 0, lc)
        no_cache_in += _no_cache_cost(sol, inp, 0, lc)
        wr = sol["cache_write"] if sol["cache_write"] is not None else sol["input"]
        write_cost += write * (wr * _lc(sol["model"], lc)["cache_write"]) / M
    return {"actual": actual, "no_cache": no_cache, "saved_by_cache": no_cache - actual,
            "astra_same_token": astra_cost, "saved_vs_astra": astra_cost - actual,
            "actual_input": actual_in, "no_cache_input": no_cache_in, "cache_write_cost": write_cost}


def _requests_of(turn: dict, inp: int, cached: int, write: int, out: int, threshold: Optional[int]) -> tuple[list[tuple], bool]:
    """The per-request breakdown of a turn when Codex reported it and it adds up to the turn's totals (then the
    long-context rate can be applied per request); else the turn as ONE request without any long-context rate."""
    raw = turn.get("requests_json")
    if raw:
        try:
            reqs = json.loads(raw) if isinstance(raw, str) else raw
            parts = [(max(int(r["i"]), 0), max(int(r.get("c") or 0), 0), max(int(r.get("w") or 0), 0), max(int(r.get("o") or 0), 0))
                     for r in reqs]
        except (ValueError, TypeError, KeyError):
            parts = []
        if parts and (sum(p[0] for p in parts), sum(p[1] for p in parts), sum(p[3] for p in parts)) == (inp, cached, out):
            out_parts = []
            for i, c, w, o in parts:
                c = min(c, i)
                w = min(w, i - c)
                out_parts.append((i, c, w, o, bool(threshold and i > threshold)))
            return out_parts, True
    return [(inp, cached, write, out, False)], False


def turn_efficiency(turn: dict, task_model: Optional[str] = None, task_tier: Optional[str] = None) -> dict:
    """Efficiency of one turns row. `usage_exact` is False for turns that did not complete (aborted, interrupted,
    failed): their token counts may be partial. A model/tier without a price gives `available: False`. The tier is the
    one requested for THIS turn (turns.service_tier) when it was recorded, else the task's."""
    inp = max(int(turn.get("input_tokens") or 0), 0)
    cached_raw = max(int(turn.get("cached_input_tokens") or 0), 0)
    out = max(int(turn.get("output_tokens") or 0), 0)
    write_raw = turn.get("cache_write_input_tokens")
    anomaly = cached_raw > inp
    cached = min(cached_raw, inp)  # cached > input is impossible: clamp to input and flag it
    write = min(max(int(write_raw or 0), 0), inp - cached)
    anomaly = anomaly or int(write_raw or 0) > inp - cached
    model = (turn.get("model") or task_model or "").strip()
    tier = turn.get("service_tier") or task_tier
    res = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
           "cache_write_tokens": write, "uncached_regular_tokens": inp - cached - write,
           "cache_hit_rate": cached / inp * 100 if inp else None,
           "usage_exact": (turn.get("status") or "completed") == "completed", "anomaly": anomaly,
           "model": model, "speed": SPEED_BY_TIER.get((tier or "default") or "default"), "available": False,
           "reason": UNPRICED, "service_tier": tier or "default", "long_context_requests": 0, "per_request": False}
    for unit in ("credits", "usd"):
        sol, astra = rate(model, tier, unit), rate(COMPARE_MODEL, None, unit)
        if sol is None or sol["model"] != SOL:  # only Sol is priced as the actual model
            return res
        reqs, known = _requests_of(turn, inp, cached, write, out, long_context_threshold(model))
        res[unit] = _unit_figures(sol, astra, reqs)
        res["per_request"], res["long_context_requests"] = known, sum(1 for r in reqs if r[4])
    base = res["credits"]["no_cache"]
    res["saved_percent"] = res["credits"]["saved_by_cache"] / base * 100 if base else None
    res["available"], res["reason"] = True, ""
    return res


_SUMS = ("actual", "no_cache", "saved_by_cache", "astra_same_token", "saved_vs_astra", "actual_input", "no_cache_input",
         "cache_write_cost")


def _bucket() -> dict:
    return {"turns": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0,
            "credits": dict.fromkeys(_SUMS, 0.0), "usd": dict.fromkeys(_SUMS, 0.0)}


def _add(b: dict, e: dict) -> None:
    b["turns"] += 1
    for k in ("input_tokens", "cached_input_tokens", "output_tokens", "cache_write_tokens"):
        b[k] += e[k]
    for unit in ("credits", "usd"):
        for k in _SUMS:
            b[unit][k] += e[unit][k]


def _finish(b: dict) -> dict:
    b["cache_hit_rate"] = b["cached_input_tokens"] / b["input_tokens"] * 100 if b["input_tokens"] else None
    base = b["credits"]["no_cache"]
    b["saved_percent"] = b["credits"]["saved_by_cache"] / base * 100 if base else None
    b["effective_input_multiplier"] = effective_input_multiplier(b["credits"])
    return b


def effective_input_multiplier(figures: dict) -> Optional[float]:
    """no-cache input cost / actual input cost, both from the rate card (cached input is priced, not free). The two
    units scale together, so credits and USD give the same ratio. None when there is no priced input."""
    return figures["no_cache_input"] / figures["actual_input"] if figures["actual_input"] else None


PERIODS = ("today", "7d", "lifetime")
PERIOD_DAYS = 7


def period_start(period: str, now: Optional[datetime] = None) -> Optional[str]:
    """Lower bound (UTC ISO, same format as turns.created_at) of an aggregation period; None = lifetime.
    `today` starts at local midnight, `7d` is the rolling last 7 x 24 hours."""
    if period not in PERIODS:
        raise ValueError(f"unknown period: {period}")
    if period == "lifetime":
        return None
    now = now or datetime.now(timezone.utc)
    if period == "today":
        start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=PERIOD_DAYS)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def aggregate(rows: Iterable[tuple[dict, Optional[str], Optional[str], bool]]) -> dict:
    """rows: (turn, task_model, task_tier, auto_approval). Token totals count every turn; savings only count turns
    with a price. Exact (completed) and estimated (usage_exact=False) turns are kept apart: `total` is both, `exact`
    only the completed turns, `estimated` only the rest."""
    exact, estimated, total = _bucket(), _bucket(), _bucket()
    tokens = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0}
    unpriced = anomalies = lc_requests = no_request_data = 0
    fast_tasks, auto_review = set(), False
    for turn, model, tier, auto in rows:
        e = turn_efficiency(turn, model, tier)
        tokens["input_tokens"] += e["input_tokens"]
        tokens["cached_input_tokens"] += e["cached_input_tokens"]
        tokens["cache_write_input_tokens"] += e["cache_write_tokens"]
        anomalies += e["anomaly"]
        auto_review = auto_review or bool(auto)
        if e["speed"] == SPEED_FAST:
            fast_tasks.add(turn.get("task_id"))
        if not e["available"]:
            unpriced += 1
            continue
        _add(exact if e["usage_exact"] else estimated, e)
        _add(total, e)
        lc_requests += e["long_context_requests"]
        no_request_data += not e["per_request"]
    notes = list(NOTES) + [ASTRA_NOTE]
    if no_request_data:
        notes.append(f"Long-context (>272K) pricing is applied only to requests whose input size Codex reported; "
                     f"{no_request_data} turn(s) have no per-request sizes, so it is not applied to them.")
    if lc_requests:
        notes.append(f"{lc_requests} request(s) with more than 272K input tokens were priced at the long-context rates "
                     "(input and cache x2, output x1.5).")
    if auto_review:
        notes.append(AUTO_REVIEW_NOTE)
    if estimated["turns"]:
        notes.append(f"{estimated['turns']} turn(s) did not complete; their usage is an estimate.")
    if unpriced:
        notes.append(f"{unpriced} turn(s) are not included in savings: {UNPRICED} (unknown model or service tier).")
    if anomalies:
        notes.append(f"{anomalies} turn(s) reported cached input above input; cached was clamped to input.")
    return {"pricing_version": PRICING_VERSION, "pricing_source": SOURCE,
            "turns": exact["turns"] + estimated["turns"] + unpriced, "unpriced_turns": unpriced,
            "anomalies": anomalies, "input_tokens": tokens["input_tokens"],
            "cached_input_tokens": tokens["cached_input_tokens"],
            "cache_write_input_tokens": tokens["cache_write_input_tokens"], "long_context_requests": lc_requests,
            "uncached_input_tokens": max(tokens["input_tokens"] - tokens["cached_input_tokens"], 0),
            "uncached_input_rate": ((tokens["input_tokens"] - tokens["cached_input_tokens"]) / tokens["input_tokens"] * 100
                                    if tokens["input_tokens"] else None),
            "cache_hit_rate": (tokens["cached_input_tokens"] / tokens["input_tokens"] * 100
                               if tokens["input_tokens"] else None),
            "exact": _finish(exact), "estimated": _finish(estimated), "total": _finish(total),
            "fast_tasks": len(fast_tasks), "allowance_multiplier_fast": ALLOWANCE_MULTIPLIER_FAST,
            "auto_review_possible": auto_review, "notes": notes}
