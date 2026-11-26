# Provider integration verification

Validation ran in the assigned worktree with temporary SQLite databases,
repositories, worktrees and loopback servers. No production database was opened
by tests, no production service was restarted, and main was not merged.

## Results

| Check | Result | Evidence / limits |
| --- | --- | --- |
| Final HEAD full default suite | See [PR #19 acceptance evidence](https://github.com/Kanade147359/codex-gui/pull/19) | Rerun after committing the acceptance fixes; the PR records the exact tested SHA, counts and log digests. |
| Installed-Codex runtime, app-server and lifecycle regression | **58 passed, 1 skipped, no warnings** | Includes all three previously failing tests, real read-only sandbox denial and exited-leader descendant cleanup. |
| Final settings/restart/API/provider run | 46 passed | Covers CLI-path propagation to execution/readers, restart, configuration dispatch races, legacy API compatibility, handoff and provider contracts. |
| Focused providers, recovery, Codex argv, sessions and heterogeneous workflows | 148 passed | Includes Claude fake subprocesses, actual streaming/parser/lifecycle, schema migrations and backups. |
| Final HEAD desktop/mobile/Autopilot browser regression | See PR #19 acceptance evidence | All four browser modules run together after the final commit; real Chromium with isolated servers and fake execution providers. |
| Ruff undefined-name/unused-import checks on provider/lifecycle/role modules | PASS | `F821,F822,F823,F401`; JavaScript syntax checks also passed. |
| Installed Claude metadata | CHECKED, not execution E2E | Version 2.1.185, supported help flags/effort/modes, subscription auth classification. No credentials persisted in GUI DB. |
| Real Claude model E2E | **NOT_RUN** | Extra usage / purchased-credit disablement cannot be verified automatically and was not confirmed for this run. Opt-in test skips by default. |
| Independent final subagent code review | **NOT_RUN** | Requested again for final acceptance; creation was rejected with `agent thread limit reached`. Local review is not reported as independent review. |

The final HEAD regression/browser results and SHA are recorded in the PR after
the commit, avoiding a self-referential commit hash in this document. The earlier
full run after socket isolation returned 1042 passed / 39 skipped; the subsequent
app-server cleanup changes require the final rerun and are covered by the
58-test focused run above. Counts are individual runs, not sums of repeated runs.

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

## Resolved installed-Codex environment failures

These three tests fail on the unchanged baseline commit
`c70cb894428484423590c045ed08da7e0ec804c6` as well as on this branch:

1. `tests/test_ctx_e2e.py::test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded`
2. `tests/test_ctx_e2e.py::test_real_codex_tool_output_limit_reaches_the_model`
3. `tests/test_ctx_integration.py::test_tool_output_token_limit_only_lowers_the_cap`

They use installed Codex against a **loopback fake Responses endpoint**, not a
billable model. Before the fix, the tool response was a short runtime failure instead of `seq`
output:

```text
error building bubblewrap command: app-server socket directory must be a user-owned directory with mode 0700
```

The enclosing execution sandbox masks `/tmp/codex-daemon-<uid>` as a mode-000
directory. A file-system syscall trace confirmed nested Codex rejected this
directory, even with global `--no-daemon`; changing `TMPDIR` did not resolve it.
The measured payload was consequently 156 characters / approximately 39 tokens
for every cap, so it could not produce the expected large-output event.

`private_codex_runtime` in `tests/conftest.py` now launches only the two real-Codex
test modules in a bubblewrap mount/PID namespace. It overlays private mode-0700
temporary directories on the daemon and synthetic-mount paths, preserves device
access, and disables shared-daemon routing. The host paths' ownership/modes and
production processes are unchanged. The CLI's actual read-only/workspace sandbox
is still enabled. A new real-tool regression checks a write is denied and no file
is created; large-output and truncation assertions are unchanged. All three
original failures now PASS. Linux without bubblewrap reports NOT_RUN rather than
running against shared host sockets.

The acceptance run also exposed app-server pipe cleanup warnings. A regression
reproduced unclosed stdin and surviving descendants when the leader had already
exited. `AppServerClient.close` now closes stdin, stops the owned process group,
waits for bounded stdout/stderr draining and awaits its reader tasks. Restart
also closes a dead server before replacing it. Both leader states and idempotent
close are tested with an actual local subprocess whose child ignores SIGTERM.
The final 58-test runtime run has no warnings.

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
python -m pytest -q tests/test_app_server_lifecycle.py tests/test_app_server.py tests/test_ctx_integration.py tests/test_ctx_e2e.py
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

## Real acceptance matrix / merge decision

The owner has been asked to confirm Extra usage and purchased-credit usage are
disabled. No confirmation was received during this acceptance run; no real Claude
model execution was started. Saved GUI settings or subscription login metadata
do not replace the requested owner confirmation.

| Required isolated real acceptance | Status | Remaining evidence |
| --- | --- | --- |
| Claude code editing and test execution | **NOT_RUN** | Actual changed files and successful test tool output. |
| Same-session continuation and GUI restart/resume | **NOT_RUN** | One persisted real Session ID across turns and a new GUI process. |
| Codex + Claude concurrent execution | **NOT_RUN** | Overlapping real runs in separate worktrees. |
| Claude native Subagent | **NOT_RUN** | Actual Agent invocation/result and GUI status. |
| Claude image attachment | **NOT_RUN** | Real visual recognition on initial/resumed messages. |
| Claude sandbox permission enforcement | **NOT_RUN** | Real denied out-of-scope write and successful allowed work. Codex's offline real-tool denial is a separate PASS. |
| Codex implementation → Claude review | **NOT_RUN** | Real providers with an isolated review and completion decision. |
| Claude implementation → Codex review | **NOT_RUN** | Real providers, including repair/re-review when applicable. |
| Completion Gate / dependency release | **NOT_RUN** | Real blocked/evidence-failed turn holds dependencies until verified success. |
| Interrupt / process collection / updater Drain | **NOT_RUN** | Real Claude process tree cleanup and isolated self-updater entry point. |
| Independent final subagent review | **NOT_RUN** | Requires an available independent-agent slot. |

Fake CLI tests cover the corresponding session, DAG, review, attachment and
shutdown state transitions; they do not establish native Claude behavior. In
particular, `test_stop_resume_timeout_and_drain` exercises `TaskManager.shutdown`,
not an end-to-end self-updater. Real updater Drain remains NOT_RUN. The existing
real-Claude test is only a new/resumed structured-output smoke test and is not a
substitute for this entire matrix.

**Merge decision: HOLD / keep draft.** Resolving the old three failures does not
satisfy the remaining real acceptance and independent-review requirements. After
owner confirmation, execute each matrix item in temporary GUI data/repositories,
fix and rerun any failures, then record the final tested SHA and evidence in PR
#19. No production restart or main merge is part of acceptance.

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
- No real self-update/restart of production was performed. Shutdown and
  backup/restore were tested with isolated application state only; an actual
  updater Drain is not established by those tests.
