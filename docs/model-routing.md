# Terra-first automatic routing (opt-in)

Select **Auto** explicitly when creating an ordinary task (`model: "auto"`).
The form keeps the existing pinned recommendation by default. Existing tasks
and Autopilot-owned tasks are not silently enrolled.

## Model policy

The role mappings `CODEX_GUI_TERRA_MODEL`, `CODEX_GUI_SOL_MODEL`, and
`CODEX_GUI_ASTRA_MODEL` default to `gpt-6.1-terra`, `gpt-6.1-sol`, and
`gpt-6-astra`. These are configurable candidates, not a claim of availability:
only models advertised by the installed `codex debug models` are eligible.
Auto requires Terra and at least one later model; labels show the actual chain.
The selected target/effort is checked again immediately before execution.

Automatic promotion requests at most `high` reasoning and Standard speed.
`xhigh`, `max`, and `ultra` are never implicitly selected. Missing models,
incompatible efforts, and failed catalog reads stop safely, without API billing
fallback or sandbox changes.

## Completion and handoff

A completed structured `PARTIAL` with an `incomplete` outcome and usable
Completion Gate evidence can advance one stage. The chain has at most three
models and two promotions. Execution failures retain the existing bounded
recovery policy; invented CLI kinds such as `max_turns` are not routing signals.
Quota, auth, infrastructure failures, missing validation, `BLOCKED`,
`NEEDS_INPUT`, manual approval, and explicit Stop do not trigger promotion.
Stop cancels routing; application shutdown preserves consent for recovery.

A promotion keeps the task, branch and worktree but creates a new Codex thread.
The handoff includes the ordered owner requests (including later corrections),
reattaches their images, and carries bounded, credential-redacted diagnostic
excerpts. A user-requested new session resets which owner history is relevant.
Oversized histories or excessive images pause routing rather than silently
losing requirements. Full conversations, diffs and AGENTS.md are not copied;
the worker must inspect the existing worktree and preserve correct work.
Final output still uses the existing structured completion schema.

Queuing and audit insertion are one transaction. Compare-and-swap checks the
completion snapshot, current route, stop/manual-approval flags, worktree state
and absence of an open attempt. A stale scheduler decision cannot overwrite a
new owner request or cancelled route. Dependents wait for genuine success.

## Metrics

`/model-performance` and `GET /api/model-performance` show model-attributed
attempts, finished/in-flight counts, duration, tokens, and currently accepted
tasks. Success rate is **completed / finished attempts**, not task quality;
in-flight attempts are excluded from the denominator. Durations retain
subsecond precision. Model identity is labelled reported, requested, or
unknown; an empty configured model is never guessed. Acceptance is attributed
to the final attempt, not a mutable task setting. Manual approvals are separate.

`GET /api/tasks/{id}/model-routes` exposes the route audit trail.
`POST /api/tasks/{id}/accepted-pr` with `pr_url` and `evidence` records **manual**
acceptance evidence for a gate/owner-approved task. Canonical PR URLs count once
across tasks; retries are idempotent and another task cannot claim the same PR.
These records are not verified GitHub merges. Historical usage/model identity
is not backfilled from current settings, and subscription usage is not priced
as API spend.

## Validation

```sh
python -m pytest -q tests/test_model_routing.py tests/test_model_routing_safety.py tests/test_database.py tests/test_recovery.py
CODEX_GUI_BROWSER=1 python -m pytest -q tests/test_model_routing_browser.py tests/test_ui_mobile_browser.py
```

All automated worker tests use fake Codex and isolated state. A real installed
Codex smoke test remains necessary before production rollout. Follow AGENTS.md
for production isolation and repository safety.
