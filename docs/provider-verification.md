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
| Final HEAD desktop/mobile/Autopilot browser regression | See PR #19 acceptance evidence | All modules containing opt-in browser tests run together after the final commit; real Chromium with isolated servers and fake execution providers. |
| Ruff undefined-name/unused-import checks on provider/lifecycle/role modules | PASS | `F821,F822,F823,F401`; JavaScript syntax checks also passed. |
| Installed Claude metadata | CHECKED, not execution E2E | Version 2.1.185, supported help flags/effort/modes, subscription auth classification. No credentials persisted in GUI DB. |
| Real Claude model E2E | **PASS** | Owner confirmation received 2026-11-27; official 2.1.185 / Sonnet 4.6, private staged OAuth, real new/resumed turns. |
| Independent final subagent code review | See PR #19 acceptance evidence | Claude native review stopped at the included-plan limit with no completed reports. A separately selected Codex native review uses a new session, fresh included-plan eligibility and a read-only clone; no automatic fallback. |

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

One separate installed-Codex check remains SKIP:
`test_profile_on_the_real_config_never_adds_tools_and_removes_apps` cannot
initialize an in-process app-server with the protected real CODEX_HOME
(`Read-only file system`). This leaves the user's real-config catalog-reduction
measurement unverified; it does not invalidate the isolated catalog/tool-output
tests. Reproduce with that node in `tests/test_ctx_integration.py`. The production
config/state permissions are intentionally not changed to make it run.

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
CODEX_GUI_BROWSER=1 CODEX_GUI_CHROMIUM=/path/to/chrome python -m pytest -q tests/test_autopilot_workflow_ui.py -k browser
python -m pytest -q tests/test_real_claude.py
```

Real Claude E2E is opt-in with **both** `CODEX_GUI_REAL_CLAUDE=1` and
`CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED=1`, after the owner independently checks
subscription billing and disables extra usage/purchased credits. The test also
requires fresh CLI metadata to confirm a subscription route; otherwise it reports
NOT_RUN. It uses temporary GUI data and a temporary repository. No API-key or
provider fallback is allowed.

## Real acceptance matrix / merge decision

The owner confirmed additional paid usage is disabled on 2026-11-27. Fresh
CLI metadata confirmed the first-party subscription route. All actual CLI state,
OAuth staging, GUI SQLite databases, repositories and worktrees are temporary;
account credentials are removed at teardown. No production data/processes or
permissions are changed. Remaining quota and invoice charges stay Unknown.

The real cases were rerun after integrating current main. The final HEAD,
per-case reruns and exact evidence are recorded in PR #19.

| Isolated real acceptance | Result | Evidence |
| --- | --- | --- |
| Claude code editing and test execution | PASS | Native Agent changes calculator.py; parent Bash runs unittest; unchanged assertions and Completion validation pass. |
| Same-session continuation and GUI restart/resume | PASS | One UUID and worktree across image follow-up and a new GUI process; subtract and its test added after restart. |
| Codex + Claude concurrent execution | PASS | Both real shell commands create separate worktree artifacts and wait concurrently on a test-owned release signal. |
| Claude native Subagent | PASS | Actual Agent invocation/result, completed native status, real tool output. |
| Claude image attachment | PASS | NATIVE742 recognized from the attached PNG and persisted in message history. |
| Claude sandbox permission enforcement | PASS | A parent-writable test path outside the task worktree fails inside native Bash with read-only filesystem; no fallback or edit-tool retry. |
| Desktop/mobile real-task GUI display | PASS | Chromium at widths 375/1280 shows Claude and completed native Subagent status. |
| Codex implementation → Claude review | PASS earlier; latest rerun blocked | Both directions passed after the Python -B fixture repair. A later integrated run reached Claude quota and waited without fallback; it is not scored PASS. Exact SHAs are recorded in PR #19. |
| Claude implementation → Codex review | PASS earlier; latest whole-workflow rerun incomplete | Earlier real Autopilot APPROVE used a distinct thread; the latest combined run could not finish after Claude quota exhaustion. |
| Completion Gate / dependency release | PASS | Missing evidence holds a real Codex dependent with zero attempts; same-Claude-session repair releases exactly one execution. |
| Interrupt / native child collection / GUI shutdown | PASS | Stop and real GUI SIGTERM collect shell descendants; preserved sessions resume after stop/restart. |
| Dedicated self-updater Drain | NOT_RUN | No updater entry point identified in this checkout; owner asked for the normal update command. GUI shutdown is a separate real PASS. |
| Independent final subagent review | See PR #19 | Claude native review started two agents but hit quota before reports. A separate Codex native review, selected explicitly, records its snapshot, complete path coverage and findings before any PASS claim. |

### Actual failures and repairs

- The latest combined real run has four PASS and one blocked Autopilot test;
  the independent Claude review also stopped at the same included-plan limit.
  Native `rate_limit_event` reported `status: rejected`,
  `overageDisabledReason: org_level_disabled` and `isUsingOverage: false`.
  Review reports were absent, so independent review is not inferred from agent
  startup. No paid retry, API route or automatic provider fallback was used.
- Codex 0.159.2 also emits v2 subAgentActivity notifications without report
  bodies. The review harness checks started/completed child IDs, then reads only
  final reports from its temporary CLI session records, validating parent thread,
  child UUID, depth and worktree. A separate regression rejects commentary and
  mismatched lineage; this does not alter the application's Completion parser.
- GitHub CI without an installed Codex exposed a backup fixture that handled
  regular files with FakeRunner but delegated image-capability discovery to a
  real binary. ReadingRunner now inherits the existing ImageRunner fixture.
  All backup/hash/reservation/restore assertions are unchanged. The 44 file and
  artifact tests pass with installed Codex deliberately excluded from PATH.

- Linux lacked socat. The enforced sandbox refused startup before a model call.
  An Ubuntu socat package was extracted under `/tmp` and added only to the test
  PATH. System packages/permissions and failIfUnavailable were not changed.
- CLI 2.1.185 can emit exact Completion JSON in terminal result without
  structured_output. The provider now passes only that entire terminal string
  through the existing strict parser. Duplicate keys, prose/fences, blank reasons
  and extra fields remain rejected; malformed structured_output cannot fall back.
- A resumed model response included prose before JSON. Completion Gate correctly
  held it. No tail extraction was added. Every CLI invocation now carries the
  completion protocol as an appended system instruction; the restart acceptance
  prompt explicitly requests StructuredOutput. Models can still be noncompliant,
  in which case the gate continues to hold them.
- Complex multiline inline Python required Claude permission approval, and a
  shutdown witness outside the worktree was denied by the native sandbox.
  Trusted fixture scripts and in-worktree witness files exercise the intended
  operations without weakening CLI permissions or sandbox policy.
- A shell-wrapper CLI path was inaccessible inside the independent Codex review
  sandbox. The test GUI now owns private runtime mounts and hands reviewers the
  real CLI executable. Both actual review directions passed after this fix.
- After main integration, a model self-check created __pycache__ outside the
  approved edit path. The existing ownership gate correctly blocked the node.
  The fixture now explicitly requires Python -B for self-checks and validation;
  edit ownership assertions and rejection of unowned files are unchanged.
- An initial Explore review was incomplete and included a Fast-tier normalization
  false positive. The UI hides unsupported Claude controls and the API rejects
  Fast; normalization to default is intentional. That review is not marked PASS.
  Follow-up reviewers use explicit Sonnet / very-thorough exploration and must
  inspect every assigned path. Documentary fenced review JSON is parsed separately
  from the strict outer Completion Contract, retaining findings and unverified
  scope and rejecting duplicate/ambiguous reports.
- Test diagnostics originally assumed capitalized shell errors and a non-null
  raw event; zsh uses lowercase and system log events have no raw payload. The
  harness normalizes case, checks actual tool results and handles absent events;
  denial/no-artifact and semantic success assertions remain unchanged.

Reproduce with bubblewrap and socat on PATH and the owner's confirmation:

```bash
CODEX_GUI_REAL_CLAUDE=1 CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED=1 python -m pytest -q -s -x tests/test_real_claude.py
CODEX_GUI_REAL_CLAUDE=1 CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED=1 CODEX_GUI_CHROMIUM=/path/to/chrome python -m pytest -q -s -x tests/test_real_provider_acceptance.py tests/test_real_provider_workflows.py
CODEX_GUI_REAL_CLAUDE=1 CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED=1 CODEX_GUI_REAL_FINAL_REVIEW=1 python -m pytest -q -s -x tests/test_real_provider_review.py
```

Codex model calls additionally require fresh included-plan usage confirmation;
unknown availability skips rather than using purchased credits or API billing.
Native final review uses isolated repository clones and independent read-only
agents, not implementation-session self-review. Select a separate Codex review
explicitly with `CODEX_GUI_FINAL_REVIEW_PROVIDER=codex`; it checks fresh ChatGPT
included-plan limits and creates a new thread, never resumes a Claude session. Its full coverage/results are saved as
JSON evidence and reviewed before a merge recommendation.

**Merge decision: HOLD until outstanding acceptance is resolved.** A dedicated
self-updater Drain remains NOT_RUN because its entry point is absent. Claude
quota prevents completing the latest Autopilot rerun. Final regression/browser
counts, independent review outcome and tested SHA are authoritative in PR #19;
an earlier PASS is never substituted for a blocked latest run. Current main
c51e99d92e6d81620acc86cf3ad6b3c8ff3477c0 is integrated on the assigned feature
branch. Conflict resolution retains provider/session fields alongside Artifact
references, file ownership and the transactional DB migration. The main branch
and production worktree are unchanged. The final committed HEAD is rerun through
the full offline regression and desktop/mobile browser suites before reporting.

## Known constraints / remaining acceptance

- Real acceptance is version/model/environment specific: Claude 2.1.185, Sonnet
  4.6 and Linux bubblewrap/socat. Other versions/models are not inferred to PASS.
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
- Release records in `docs/releases/gui-release-01.json` and
  `docs/releases/gui-release-01-artifact.json` also identify self-update as absent
  and defer it to a separate update task; no replacement updater was invented.
- No real self-update/restart of production was performed. Shutdown and
  backup/restore were tested with isolated application state only; an actual
  updater Drain is not established by those tests.
