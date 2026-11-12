"""Evidence-derived counters; absent telemetry is unknown, never an estimated zero."""

from datetime import datetime


def rate(success, total):
    return round(success / total, 4) if total else None


def elapsed(start, finish):
    if not start or not finish:
        return None
    return max(0, (datetime.fromisoformat(finish.replace("Z", "+00:00"))
                   - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())


def summarize(ledger, view):
    nodes = view["nodes"]
    candidates = [c for n in nodes for c in n["candidates"]]
    validations = [v for c in candidates for v in c["validation"] if v["state"] in ("PASS", "FAIL")]
    audits = [r for c in candidates for r in c["reviews"] if r["provider"] == "claude"]
    attempts = [a for n in nodes if n["task_id"] for a in ledger.db.list_attempts(n["task_id"])]
    dispatches = [e for e in view["events"] if e["kind"] == "dsh"]
    durations = {n["name"]: sum(d for a in attempts if a["task_id"] == n["task_id"]
                              if (d := elapsed(a["started_at"], a["finished_at"])) is not None)
                 for n in nodes if n["task_id"]}
    checks = [check for v in validations for check in v["payload"].get("checks", [])]
    return dict(
        codex_executions=len(attempts),
        # An exit status is deliberately not used as semantic-success evidence.
        codex_success_rate=rate(sum(n["state"] == "accepted" for n in nodes),
                                sum(bool(n["task_id"]) for n in nodes)),
        codex_success_rate_basis="accepted tasks / started tasks, including tasks still in progress",
        claude_audits=len(audits),
        claude_findings=sum(len(r["payload"].get("blocking", [])) + len(r["payload"].get("nonblocking", []))
                            for r in audits),
        dsh_decisions=len(dispatches),
        dsh_request_attempts=sum(e["payload"].get("attempt", 1) for e in dispatches),
        dsh_assignment_success_rate=rate(sum(e["state"] == "completed" for e in dispatches), len(dispatches)),
        validation_pass_rate=rate(sum(v["state"] == "PASS" for v in validations), len(validations)),
        automated_check_pass_rate=rate(sum(c.get("exit_status") == 0 and not c.get("timed_out") for c in checks), len(checks)),
        verified_deliverables=sum(len(c["acceptances"]) for c in candidates),
        human_decisions=sum(len(n["decisions"]) for n in nodes),
        task_execution_seconds=durations,
        automatic_recovery_success_rate=None,
        quota_stop_seconds=None,
        telemetry_limits=["Recovery success and quota wait intervals are not yet instrumented.",
                          "Execution seconds sum finished attempts; active attempts are excluded.",
                          "Human decisions exclude pause/resume controls and historical external intervention."],
        dsh=[dict(state=e["state"], **e["payload"]) for e in dispatches],
    )
