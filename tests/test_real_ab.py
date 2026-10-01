"""Opt-in A/B on the REAL model: Codex's default settings vs the Context Efficiency settings, same small task.

    CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_ab.py -s

It spends real (subscription) usage: ~4 small tasks. It reports, per run: input, cached input, cache writes, output, tool
calls, large tool outputs and whether the answers were right. It asserts only that every run finished and answered
correctly; it never asserts a saving, because a setting that does not save, or that costs quality, must show up as such.
Runs alternate A, B, A, B so that warm-up and cache sharing between runs do not favour one side.
"""
import asyncio
import csv
import os
import random
import re
import subprocess
from collections import Counter

import pytest

from app.task_manager import TaskManager

from conftest import wait_for

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_REAL") != "1", reason="set CODEX_GUI_REAL=1 to use the real codex")

MODEL = os.environ.get("CODEX_GUI_REAL_MODEL", "gpt-6.1-sol")
ROUNDS = int(os.environ.get("CODEX_GUI_REAL_AB_ROUNDS", "2"))
SETTINGS = {
    "A default": dict(allow_subagents=True, tool_output="default", skills="default", tool_profile="full"),
    "B efficiency": dict(allow_subagents=False, tool_output="conservative", skills="economy", tool_profile="development"),
}
PROMPT1 = ("In data/events.csv (columns: id,user,latency_ms,status) report: (1) the number of data rows, (2) the three most frequent "
           "`user` values with their counts, (3) the maximum latency_ms and the id of that row. Do not modify any file. "
           "Answer in three lines, formatted `rows=N`, `top=user:count,user:count,user:count`, `max=latency:id`.")
PROMPT2 = "Now report how many rows have status `error`, as one line `errors=N`. Do not modify any file."


def make_repo(path):
    rnd = random.Random(7)
    (path / "data").mkdir(parents=True)
    users = [f"user{i:02d}" for i in range(25)]
    rows = [(i, rnd.choices(users, weights=range(25, 0, -1))[0], rnd.randint(5, 4000), rnd.choice(["ok", "ok", "ok", "error", "slow"]))
            for i in range(1, 6001)]
    with open(path / "data" / "events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "user", "latency_ms", "status"])
        w.writerows(rows)
    (path / "README.md").write_text("# events demo\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    top = Counter(r[1] for r in rows).most_common(3)
    worst = max(rows, key=lambda r: r[2])
    return {"rows": len(rows), "top": top, "max": (worst[2], worst[0]), "errors": sum(1 for r in rows if r[3] == "error")}


def last_message(m, task_id):
    msgs = [e["message"] for e in m.read_log(task_id)[0] if e["type"].endswith("agentMessage")]
    return msgs[-1] if msgs else ""


def judge(answer1, answer2, truth):
    ok_rows = f"rows={truth['rows']}" in answer1.replace(" ", "")
    top = ",".join(f"{u}:{c}" for u, c in truth["top"])
    ok_top = top in answer1.replace(" ", "")
    ok_max = f"max={truth['max'][0]}:{truth['max'][1]}" in answer1.replace(" ", "")
    ok_err = f"errors={truth['errors']}" in answer2.replace(" ", "")
    return {"rows": ok_rows, "top": ok_top, "max": ok_max, "errors": ok_err}


def test_default_vs_efficiency_on_the_real_model(tmp_path, settings, db):
    settings.backend = "app-server"
    m = TaskManager(settings, db)
    results = []

    async def one(label, cfg, n):
        repo = tmp_path / f"repo-{n}"
        truth = make_repo(repo)
        t = await m.create_task(repository=str(repo), prompt=PROMPT1, name=f"ab-{label}-{n}", model=MODEL, reasoning_effort="low", **cfg)

        async def done(turns):
            row = await wait_for(lambda: m.get(t["id"])["status"] not in ("queued", "starting", "running") and m.get(t["id"]), 600)
            assert row["status"] == "completed", row["status_detail"]
            assert len(m.db.list_turns(t["id"])) >= turns

        await done(1)
        a1 = last_message(m, t["id"])
        await m.send_instruction(t["id"], PROMPT2, service_tier="standard")
        await done(2)
        a2 = last_message(m, t["id"])
        rows = m.db.list_turns(t["id"])
        eff = m.usage(t["id"])["efficiency"]
        results.append({
            "setting": label, "n": n, "input": sum(r["input_tokens"] for r in rows), "cached": sum(r["cached_input_tokens"] for r in rows),
            "write": sum(r["cache_write_input_tokens"] or 0 for r in rows), "output": sum(r["output_tokens"] for r in rows),
            "tool_calls": sum(r["tool_calls"] or 0 for r in rows), "large": sum(r["large_tool_outputs"] or 0 for r in rows),
            "requests": sum(r["requests"] or 0 for r in rows), "usd": eff["total"]["usd"]["actual"],
            "correct": judge(a1, a2, truth), "answers": (a1[-300:], a2[-120:]),
            "tool_outputs_tokens": sum(r["tool_output_tokens_est"] or 0 for r in rows)})

    async def scenario():
        n = 0
        for _ in range(ROUNDS):
            for label, cfg in SETTINGS.items():
                n += 1
                await one(label, cfg, n)
        await m.shutdown()

    asyncio.run(scenario())
    print("\n" + f"{'setting':14s} {'run':>3} {'input':>8} {'cached':>8} {'write':>7} {'output':>6} {'uncached':>8} {'reqs':>4} {'tools':>5} {'large':>5} {'usd-eq':>8}  correct")
    for r in results:
        un = r["input"] - r["cached"]
        print(f"{r['setting']:14s} {r['n']:>3} {r['input']:>8,} {r['cached']:>8,} {r['write']:>7,} {r['output']:>6,} {un:>8,} {r['requests']:>4} {r['tool_calls']:>5} {r['large']:>5} {r['usd']:>8.4f}  {r['correct']}")
    for r in results:
        print(r["setting"], r["n"], "->", repr(r["answers"][0][-160:]), "|", repr(r["answers"][1][-60:]))
    assert len(results) == ROUNDS * len(SETTINGS)
    assert all(re.search(r"rows", r["answers"][0]) for r in results)
