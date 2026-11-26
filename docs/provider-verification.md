# Provider integration verification

Validation ran in the assigned worktree with temporary SQLite databases,
repositories, worktrees and loopback servers. No production database was opened
by tests, no production service was restarted, and main was not merged.

## Results

| Check | Result | Evidence / limits |
| --- | --- | --- |
| Full default suite | 1031 passed, 3 failed, 39 skipped | Three installed-Codex environment failures below; never reported as PASS. |
| Final settings/restart/API/provider run | 46 passed | Covers CLI-path propagation to execution/readers, restart, configuration dispatch races, legacy API compatibility, handoff and provider contracts. |
| Focused providers, recovery, Codex argv, sessions and heterogeneous workflows | 148 passed | Includes Claude fake subprocesses, actual streaming/parser/lifecycle, schema migrations and backups. |
| Desktop/mobile task/browser regression | 14 passed | Real Chromium; new Claude task, image upload, same-session follow-up and scheduling; existing mobile/attachment flows. |
| Autopilot browser regression | 3 passed | Real Chromium; independent review/fallback, Markdown revision/fix/integration and owner-approved replanning. |
| Ruff undefined-name/unused-import checks on provider/lifecycle/role modules | PASS | `F821,F822,F823,F401`; JavaScript syntax checks also passed. |
| Installed Claude metadata | CHECKED, not execution E2E | Version 2.1.185, supported help flags/effort/modes, subscription auth classification. No credentials persisted in GUI DB. |
| Real Claude model E2E | **NOT_RUN** | Extra usage / purchased-credit disablement cannot be verified automatically and was not confirmed for this run. Opt-in test skips by default. |
| Parallel development/review subagents | NOT_RUN | Requested subagent creation was rejected by the environment's agent-thread limit. Implementation/review proceeded serially. |

The full-suite measurement precedes the final explicit provider-handoff and CLI
settings propagation adjustments; those changes are covered by the final
API/provider-focused run listed above.
Earlier failures found during implementation were fixed and rerun; the numbers
above are successful final runs for each listed group, not totals of repeated runs.

## Coverage

The fake Claude CLI never calls an external service or loads account credentials.
It exercises:

- New task, resume UUID persistence, GUI-manager restart, scheduled FIFO dispatch,
  stop/shutdown, timeout and bounded unexpected-crash recovery in the same session.
- Mixed Codex app-server + Claude process execution, provider concurrency limits,
  provider quota queue pause and explicit queue resume.
- Text deltas, tools/results, subagent status, unknown events, malformed lines,
  duplicate JSON fields/results, wrong/missing sessions, missing terminal output.
- Image stdin payloads and image/message history on initial and resumed turns.
- Auth expiry, missing CLI, unknown billing, unexpected overage, denied permission,
  forbidden bypass options, environment separation and log credential redaction.
- Repository trust and billing checks before worktree creation; checkout hooks do
  not execute for Claude worktrees, including when creation is rejected.
- Structured SUCCESS/BLOCKED/NEEDS_INPUT/PARTIAL, missing required artifacts,
  unchanged worktree requirements and manual completion approval. A blocked
  Claude parent holds a Codex dependent until a verified successful continuation.
- Codex implementation → Claude review; Claude implementation → Codex review →
  same-Claude-session repair; explicit role routing and no unauthorized fallback.
- Research report nodes with both providers, independent worktrees, shared DAG and
  completion/evidence rules. Reader roles reject unsupported tool/subagent options.
- Legacy/fresh/idempotent migration, injected migration failure rollback, snapshot
  restore, preserved provider/session/dependencies/settings and task history.
- A child inheriting pipes after the CLI leader exits is terminated before the
  task completes. Stop targets only the owned process group.

## Existing installed-Codex environment failures

These three tests fail on the unchanged baseline commit
`c70cb894428484423590c045ed08da7e0ec804c6` as well as on this branch:

1. `tests/test_ctx_e2e.py::test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded`
2. `tests/test_ctx_e2e.py::test_real_codex_tool_output_limit_reaches_the_model`
3. `tests/test_ctx_integration.py::test_tool_output_token_limit_only_lowers_the_cap`

They use installed Codex against a **loopback fake Responses endpoint**, not a
billable model. The tool response is a short runtime failure instead of `seq`
output:

```text
error building bubblewrap command: app-server socket directory must be a user-owned directory with mode 0700
```

Consequently the measured tool payload has 156 characters / approximately 39
tokens regardless of the requested cap, and cannot produce a large-output event.
No assertions were weakened. Baseline reproduction used `git archive` into an
isolated `/tmp` directory, then the same three pytest node IDs. Production socket
ownership/modes were not changed. Remaining work is to repair or replace the
**test-only** Codex runtime/socket environment and rerun these measurements; this
PR does not claim those tests passed.

One run also saw an empty result in
`test_default_allows_spawn_and_multi_agent_false_alone_does_not_stop_it`.
Individual baseline/current reruns and the latest full suite passed. Its cause
remains unconfirmed. The test now writes `spawn-probe-diagnostic.json` under its
temporary repository parent if it recurs, preserving return code, request count,
overrides and bounded tool outputs without weakening the original assertions.

## Reproduction

Install project test dependencies and optionally Playwright in an isolated venv.
Commands used (replace the venv/Chromium locations as needed):

```bash
python -m pytest -q
python -m pytest -q tests/test_providers.py tests/test_recovery.py tests/test_codex_runner.py tests/test_sessions.py tests/test_autopilot_providers.py
python -m pytest -q tests/test_provider_api.py tests/test_providers.py tests/test_api.py
CODEX_GUI_BROWSER=1 CODEX_GUI_CHROMIUM=/path/to/chrome python -m pytest -q tests/test_provider_browser.py tests/test_ui_mobile_browser.py tests/test_ui_attachments_browser.py
CODEX_GUI_BROWSER=1 CODEX_GUI_CHROMIUM=/path/to/chrome python -m pytest -q tests/test_autopilot_browser.py
python -m pytest -q tests/test_real_claude.py
```

Real Claude E2E is opt-in with **both** `CODEX_GUI_REAL_CLAUDE=1` and
`CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED=1`, after the owner independently checks
subscription billing and disables extra usage/purchased credits. The test also
requires fresh CLI metadata to confirm a subscription route; otherwise it reports
NOT_RUN. It uses temporary GUI data and a temporary repository. No API-key or
provider fallback is allowed.

## Known constraints / remaining acceptance

- Real Claude execution, image/model compatibility, native subagent behavior and
  native Bash sandbox enforcement remain **unverified**. Metadata and synthetic
  protocol fixtures do not establish them. Keep the PR in draft until the
  no-extra-usage real E2E can be performed in a suitable local environment.
- CLI settings are pinned for safe-mode execution; repository/user Claude
  customizations are intentionally not loaded. Managed organization policies
  still have precedence, and remote managed policy is not fully introspectable.
- Only foreground built-in implementation subagents are enabled. Isolated
  reviewers/planners/mentors have no tools/subagents. Background tasks are off;
  multiple terminal result envelopes fail closed rather than being guessed at.
- Remaining quota and actual subscription charges are Unknown. CLI-reported USD
  cost is not a measured invoice charge. Subscription extra-usage disablement is
  an explicit owner assertion, not an automatically verified account setting.
- Existing workflows without explicit `allow_provider_fallback` now wait at
  included-quota exhaustion rather than silently choosing another provider.
- No real self-update/restart of production was performed. Drain/shutdown and
  backup/restore were tested with isolated application state only.
