# Document workflow verification — 2026-10-15

Code/test SHA: `a5a9daad8dc4c99b00a6655e68044fdd52cba6bc`.
Base: `12dabe5385f130383b00cd2609dd6bff7360d68f` (main includes earlier PR #3 and review coverage fixes #4).
Draft PR: https://github.com/Kanade147359/codex-gui/pull/5 .

Status: **IMPLEMENTED_UNMERGED**, **VERIFIED_WITH_FAKE_PROVIDERS**,
**REAL_MODEL_NOT_RUN**, **NOT_DEPLOYED**. No operational acceptance of real model
quality or billing is inferred from fixture success.

## Verified behavior

The public [Markdown fixture](../../tests/fixtures/autopilot-documents) goes through
actual multipart import, editable conversion, dry-run and Start APIs. The GUI/SQLite
connection is restarted after durable 00 acceptance, then its accepted planning
artifact concretizes A/B/C. A omits a condition present only in COMMON.md; independent
review rejects it, its original Codex thread fixes it, and a fresh candidate is
validated/reviewed. B hits a typed Claude included-quota fixture and the same candidate
is independently reviewed by Codex. I1 merges accepted SHA inputs, executes a real
integration implementation turn even without conflicts, and writes wiring.py. D starts
at I1's post-implementation accepted SHA and reads the pinned handoffs inside its sandbox.
After Start there are zero manual Send, Approve or merge operations in this scenario.

[Compact E2E evidence](document-workflow-e2e.json) records candidate generations,
full base/head/tree SHAs, requirement/artifact digests, every required validation
exit status, review sessions/findings/fallback and accepted integration inputs.
It excludes model conversation and credentials. The source/reference document in
this repository fixture is 1,400,000 bytes and unchanged; it never enters review context.

The actual Chromium upload flow also completes through 00, revision, A/B/C,
review/fix/fallback, integration implementation and D. Mobile checks include 390 px
Autopilot and existing 320 px controls. Browser JavaScript errors and overflow fail
the tests. Its server binds an ephemeral localhost port and uses only temporary data.

## Regression and compatibility checks

- 71 focused regressions/smokes pass, including the existing quota fallback,
  same-thread repair, Stop, normal dependencies, duplicate claims and old manifests.
- 4 browser checks pass (9 unrelated mobile cases deselected).
- Full suite: **799 passed, 29 skipped, 3 existing failures**. All three reproduce
  on unchanged base main; no new failure was found. Exact commands, summaries and
  log digests are in [the machine-readable result](document-workflow-verification.json)
  and [test summaries](document-workflow-test-results.txt).
- The 16 document test cases cover import safety, Draft refinement, owner expansion gates,
  stale/duplicate decisions, a second human question on the same candidate, actual DB
  restart, immutable requirements, bounded snapshot reads, all-partition rejection,
  conflict-free/conflicted integration turns and merge-crash reconciliation.
  Final candidate approval is separate from design approval under both policy settings.
  The design-gated successor starts only after final candidate acceptance.
- UTF-8 multi-byte diff partitions preserve every byte. CRLF Markdown import is
  normalized; legacy JSON document bytes and artifact digests remain exact.
- Validator tests prove shared build outputs survive between required commands while
  source stays unchanged, and a scratch symlink cannot make the host controller write
  outside its private directory.

Installed compatibility observations:

| Component | Verified | Not verified |
| --- | --- | --- |
| Claude Code 2.1.185 | Existing isolated auth/status smoke, subscription OAuth metadata, strict terminal-envelope parser with independent subprocess fixtures | Real headless model inference, actual quota exhaustion, model quality/entitlement or account extra-usage state |
| Codex CLI 0.159.2 | Installed app-server, two independent thread IDs, dynamic outputSchema including requested_reads, terminal result parsing, tool restrictions and cancellation against a localhost scripted endpoint; isolated ChatGPT account/read metadata | Real model inference or subscription quota/billing |
| Rust 1.98.1 | Actual offline cargo build in bwrap using a registered narrow read-only toolchain; binary runs from scratch | Project-specific third-party crates/real hardware |
| Node 20.17.0 / Python 3.14.4 | Actual sandbox commands, offline cache/read-only mount, persistent private output and source/GUI-data isolation | Arbitrary project dependency availability |

Toolchain probes and candidate code run under bwrap, with network disabled and no
home/GUI DB/credential/socket mounts. No installer, account changes or sandbox bypass
is used. OAuth smoke stages read-only credentials; provider tests never call real
models or consume/reset purchased credits. Typed quota examples remain fixtures,
not captured exhausted subscriptions. The owner billing confirmation has not been
supplied for real-model operational smoke; it is not silently inferred from auth.

## Existing failures

The full suite retains three installed-CLI/local-mock compatibility failures:

1. `tests/test_ctx_e2e.py::test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded`
2. `tests/test_ctx_e2e.py::test_real_codex_tool_output_limit_reaches_the_model`
3. `tests/test_ctx_integration.py::test_tool_output_token_limit_only_lowers_the_cap`

All three also fail on an unchanged archive of base main `12dabe5`, using temporary
repositories and the same installed CLI/local scripted endpoint. This is not a real
model test. Existing assertions are preserved, including exact successor fixture
contents; no expectation is relaxed to obtain a passing result.

## Operating limits and safety

Read [operations](document-workflows.md) before enabling a personal workflow.
Prepared toolchains/caches must be narrow directories without symlinks/credentials;
binary/oversized handoffs require a registered text summary or smaller artifacts.
Review requests are bounded and unsupported scopes stop explicitly. Unknown external
process outcomes stay in reconciliation; this does not claim exactly-once CLI use.
Legacy active candidates whose requirement digest predates the new input contract
cannot reuse stale approval; they retain history and require a fresh reviewed submission.

Work used only the assigned development worktree and temporary databases/repositories.
The production GUI, production DB, main checkout, billing settings and hardware were
not modified. The dedicated branch is normally pushed to a Draft PR. No main merge,
force push, service restart, deploy or live GUI update was performed.
