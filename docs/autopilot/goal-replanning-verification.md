# Goal-aware review / root-cause replanning verification

Tested implementation SHA: `654244a65dd30c0c670377005207e0d330db048d`.
Stack base / PR #5 SHA: `d395d232807203813b5052ae0df9ab926690d180`.
Date: 2026-10-15. Environment: Linux/WSL, Python 3.10.14, Codex CLI 0.159.2,
bubblewrap 0.11.1. Dependencies were installed into `/tmp/codex-goals-venv`.

## Results

| Check | Result |
| --- | --- |
| Goal/replanning, legacy Autopilot, document workflow, API and toolchain suites | **90 passed, 1 skipped** |
| Isolated Chromium Autopilot E2E (including owner graph approval and mobile layout) | **3 passed** |
| Installed Codex CLI, local scripted endpoint: legacy review, review v2, replanner schema, disabled tools/cancellation | **4 passed** |
| Entire repository at the tested implementation SHA | **823 passed, 32 skipped, 3 failed** |
| Syntax, `node --check static/autopilot.js`, `git diff --check` | PASS |
| Real subscription model implementation/review/replanning | **NOT RUN** |

The three repository-wide failures reproduce unchanged at the exact stack base:

- `tests/test_ctx_e2e.py::test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded`
- `tests/test_ctx_e2e.py::test_real_codex_tool_output_limit_reaches_the_model`
- `tests/test_ctx_integration.py::test_tool_output_token_limit_only_lowers_the_cap`

The first two base failures were reproduced by running the complete base
`test_ctx_e2e.py`: **2 failed, 4 passed**. The third was reproduced independently:
**1 failed**. The installed CLI's context/tool probes report unchanged output sizes
(the limit comparison measures 156 vs 156 characters / 39 vs 39 estimated tokens).
Those tests and the context measurement implementation were not modified to hide
the failures. These probes use a local scripted endpoint, not paid model usage.

## Reproduction

From a disposable checkout of the tested SHA:

```sh
python3 -m venv /tmp/codex-goals-venv
/tmp/codex-goals-venv/bin/pip install -r requirements-dev.txt playwright
/tmp/codex-goals-venv/bin/python -m pytest tests/test_autopilot_goals.py tests/test_autopilot.py tests/test_autopilot_documents.py tests/test_autopilot_api.py tests/test_autopilot_toolchains.py -q
CODEX_GUI_BROWSER=1 /tmp/codex-goals-venv/bin/python -m pytest tests/test_autopilot_browser.py -q
CODEX_GUI_AUTOPILOT_CLI_SMOKE=1 /tmp/codex-goals-venv/bin/python -m pytest tests/test_autopilot_cli_smoke.py -q
/tmp/codex-goals-venv/bin/python -m pytest -q
```

Chromium must be prepared before the browser command. Tests bind an ephemeral
localhost port; they never connect to production port 8765. Their data homes,
SQLite files, repositories, implementation worktrees and provider scratch homes
are temporary. `tests/test_autopilot_goals.py` contains 24 parameterized cases.
The API E2E writes the complete workflow snapshot to its pytest temporary
`goal-replanning-e2e.json`; the browser writes a mobile screenshot to its temporary
directory. No production GUI, production SQLite, main ref, existing task worktree,
existing PR, deployment or billing setting was modified.

## Scenario evidence

The [exported E2E record](goal-replanning-e2e.json) contains actual isolated
candidate/base/tree SHAs, diff digests, immutable requirement digests, command
results, reviewer classifications, sessions, repair history, proposed graph and
accepted replacement SHA consumed by the successor. It is a compact export of the
real API/SQLite workflow result, with synthetic model responses explicitly labeled.

| Required behavior | Regression / E2E |
| --- | --- |
| Individual tests pass but COMMON-only prohibition fails review | `test_goal_review_rejects_common_only_violation_and_accepts_partial_goal` |
| Partial workflow goal delivery accepted | Same test, independent C candidate |
| Simple implementation defect uses original thread without new task | Same test and legacy repair E2E |
| False premise discovered after repair; independent evidence-bound proposal | `test_api_auto_replan_executes_reviewed_replacement_before_successor` |
| First review can propose a replan | `test_first_review_can_request_root_cause_analysis` |
| Failed post-fix validation gets diagnosis before another repair | `test_post_fix_validation_failure_is_diagnosed_before_repair` |
| Authorized auto registration, review and dependency handoff | API auto-replan E2E |
| Propose-only / unauthorized scope waits, unrelated X continues | `test_owner_proposals_do_not_stop_independent_work` |
| Original never becomes SUCCESS through replanning | API E2E checks source task outcome and absence of original acceptances |
| Restart, duplicate tick, two SQLite connections, transaction rollback | `test_proposed_graph_survives_restart_and_concurrent_apply_is_idempotent`, `test_atomic_graph_commit_race_and_rollback` |
| Stop, changed SHA/plan/generation, stale/duplicate owner decision, consumed input | Stale proposal parameterization and interrupted replanner parameterization |
| Off, max source reservations, depth and task limits | Replanning budget and closed proposal-schema tests |
| Model cannot grant permissions, waive checks/gates, change COMMON or invent IDs/evidence | Closed-schema, owner-change and versioned-review tests; CLI tool isolation smoke |
| Incomplete/negative partition cannot become overall goal approval | `test_overall_goal_approval_cannot_erase_inconclusive_partition` |
| Large required context cannot be omitted to APPROVE | `test_large_required_context_never_silently_approves` |
| Claude quota fallback, repair loop, Draft planning, merge-then-implement, legacy manifests | Existing Autopilot/document/toolchain suites |

## Limits and owner decisions

This is controller/transport verification with fixture models, not evidence that a
real model will reliably infer the same root cause. The fixtures inspect exact
fixed documents, input digests, tracked observations, profiles and dependency
inputs; they do not replace a real-model evaluation. No real-model PASS is claimed.

The initial bounds are three new tasks, one reservation per source, depth one,
200 workflow nodes and 1 MiB of complete review/replanner input. New tasks retain
the source acceptance contract, mandatory validation and gates. If the original
acceptance/COMMON/permissions must change, `owner_changes` keeps the proposal
blocked; an owner-authored, reviewed workflow is required. Interrupted running
replanners remain blocked for inspection rather than starting another session.
Historical accepted nodes are retained; an in-flight v1 review cannot pass the
new acceptance gate. These are deliberate limits, not automatic approvals.
