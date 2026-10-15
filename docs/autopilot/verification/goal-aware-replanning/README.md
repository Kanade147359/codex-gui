# Goal-aware review / root-cause replanning verification

Tested code and tests: `471f1a124684fdc4c6cac87882fa338a47621742`.
Stacked Draft PR: [#7](https://github.com/Kanade147359/codex-gui/pull/7), based on
unmodified PR #5 at `d395d232807203813b5052ae0df9ab926690d180`.
Documentation-only commits follow the tested commit.

`IMPLEMENTED_UNMERGED` / `VERIFIED_WITH_FAKE_PROVIDERS` / `REAL_MODEL_NOT_RUN` /
`NOT_DEPLOYED`. Operational model quality and spending behavior are not established
by fixture or localhost scripted-response success.

Results:

- Full repository suite: **826 passed, 33 skipped, 3 failed**. All 91 goal/root-cause
  and existing Autopilot/API/document regressions pass, including 27 new focused cases.
- The same three `test_ctx_*` assertions fail on an isolated, unmodified archive of
  PR #5's fixed base with installed Codex 0.159.2. Those tests/assertions were not changed.
- Explicit opt-in checks: **13 passed**. Four Chromium UI E2Es (existing document flow
  and new auto/propose-only root-cause flows), installed CLI review-v2/replan output,
  fresh sessions, tool catalog/cancellation, subscription auth metadata, and real
  offline Rust/Node/Python sandbox builds. Tests use localhost ephemeral ports and
  temporary databases/repos. No production GUI process or data is used.
- Python static checks, JavaScript syntax and `git diff --check`: PASS.

Machine-readable [verification.json](verification.json), [full suite](full-suite.log),
[unchanged-base failures](baseline-d395d23.log), [opt-in checks](installed-cli-browser-toolchains.log),
and [synthetic E2E trace](e2e.json) retain exact SHAs, input digests, independent
session IDs, managed validation, original unsuccessful task state and graph revision.

| Requirement | Evidence |
| --- | --- |
| H1: COMMON-only prohibition despite local PASS | `test_common_only_prohibition_rejects_locally_passing_code_and_uses_original_thread` |
| H2: consistent partial goal accepted | `test_goal_schema_rejects_legacy_unknown_and_mismatched_digest` |
| H3: ordinary bug repairs in original thread | COMMON-only test and existing T08/T14; no generated node |
| H4: post-repair premise error, independent analyst | `test_root_cause_dynamic_graph_real_api_sqlite_git_and_restart`; initial-review counterpart also passes |
| H5: bounded automatic graph addition and successor | API E2E plus auto-mode browser E2E; Start followed by zero manual Send/Approve/merge |
| H6: scope expansion waits; independent branch continues | `test_replan_owner_decision_is_versioned_and_separate_from_resume` |
| H7: original is not SUCCESS; accepted replacement pins dependencies | E2E trace: C remains native needs_review, R accepted, D consumes R's exact accepted SHA |
| H8: restart, concurrent apply, Stop, stale scope/input/decision | API restart E2E, stale-target matrix, interrupted/late analyst and quota tests |
| H9: model cannot change goals/gates/tests/authority/limits | Malicious proposal matrix, unanchored quote rejection, bounded policy/schema/owner target tests; existing isolation negatives |
| H10: existing quota/partition/fix/00/integration/manifest behavior | All existing Autopilot/API/document tests and both existing browser E2Es pass |
| Actual accepted prerequisites in sandbox and analyst packet | `test_accepted_prerequisite_is_readable_to_replanner_and_managed_sandbox` |

The fake implementer, reviewer and replanner inspect the actual frozen original
bodies, scope, counterexample source views, validation/fix history, dependency inputs
and authority. Replanner proposal version 1 is distinct from CLI `provider_version`.
Clean review is NO_ISSUE; an incomplete analysis is INCONCLUSIVE. Neither can override
negative partitions or missing mandatory evidence.

Claude 2.1.185 and Codex 0.159.2 are measured installed versions. Quota classification
uses explicitly synthetic versioned events; real quotas were not exhausted. Real model
turns were NOT_RUN because owner confirmation of disabled extra usage/credits is not
established for bootstrap. Auth checks stage existing credentials privately/read-only;
they do not change originals, account settings, credits or billing routes.

PR #5, main (`12dabe5385f130383b00cd2609dd6bff7360d68f`), the assigned earlier worktree,
production SQLite, live GUI, billing and hardware remain unchanged. No merge/deploy/
restart was performed. Use the [operation guide](../../goal-aware-replanning.md) to opt
into a new workflow; existing tasks are not automatically migrated or approved.
