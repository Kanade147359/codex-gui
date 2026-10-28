# Terra-first automatic routing (opt-in)

This feature affects **new ordinary tasks** started with `model: "auto"`. Existing
tasks, user-pinned models, and Autopilot-owned tasks retain their current model
and session behavior.

## Requirements

The installed Codex CLI must advertise Terra and at least one higher-tier
model via `codex debug models`. Model slugs are never guessed at execution:
the default role mapping is `gpt-6.1-terra`, `gpt-6.1-sol`, `gpt-6-astra`,
and may be overridden with `CODEX_GUI_TERRA_MODEL`,
`CODEX_GUI_SOL_MODEL`, and `CODEX_GUI_ASTRA_MODEL`.

If Terra or every higher model is missing, Auto is unavailable and the GUI
falls back to its existing model selection. No API-billed alternative is used.

## Execution policy

1. Auto starts with Terra, in an ordinary sandboxed task worktree.
2. Ordinary automatic *infrastructure* retries still reuse the same thread,
   only for retryable process/IPC failures, subject to their existing budget.
3. A task explicitly reporting structured `PARTIAL` with an `incomplete`
   outcome, or a Codex-reported `model_error`, `max_output_tokens` or
   `max_turns` execution failure, may be upgraded to the next available
   model. A new Codex thread starts in the **same** worktree and branch.
4. Authentication, quota, unknown/transient failures, manual stops,
   `BLOCKED`, `NEEDS_INPUT`, and `needs_review` do **not** trigger model
   upgrades. The route has a finite length; no recursive loop is possible.
5. The handoff carries limited execution diagnostics, prior result, and
   failure hypothesis as untrusted data. The new model must inspect existing
   git diff, commits, and tests, and must never reset/clean the worktree.

Route changes are transactionally committed with an audit event and a
compare-and-swap on status, current model, and route index.

## APIs and metrics

- `GET /api/options` exposes `auto_route_chain` for GUI eligibility.
- `GET /api/tasks/{id}/model-routes` exposes the route audit trail.
- `GET /api/model-performance` exposes per-model completed attempts,
  execution success ratio, observed duration, per-turn usage, and
  Completion Gate-approved tasks.
- `/model-performance` displays the model statistics.
- `POST /api/tasks/{id}/accepted-pr` with
  `{"pr_url":"https://github.com/owner/repo/pull/123","evidence":"..."}`
  records **manual evidence**, once per (task, PR URL), after the task passed
  its Completion Gate. This is not GitHub merge verification, and is labelled
  separately to avoid inflating accepted PR counts.

Metrics do not estimate paid API spend from subscription usage. Historical
attempts that lack model attribution remain unknown rather than guessed.

## Validation

```bash
python -m pytest -q tests/test_model_routing.py tests/test_database.py tests/test_recovery.py
```

Keep production worktrees, sessions, databases and ports untouched during
tests. Before enabling on production, smoke-test one deliberately PARTIAL task
with the installed Codex CLI and verify exactly one route event and a new
thread on the same branch.
