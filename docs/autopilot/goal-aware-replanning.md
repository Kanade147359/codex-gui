# Goal-aware review and root-cause replanning

Start from the [Markdown workflow instructions](document-workflows.md). This
extension uses the same importer, fixed input bundles, controller, TaskManager,
independent review providers, validation profiles, owner decisions and SQLite
ledger. It adds no production deployment or billing permission.

## Owner operation

1. Upload COMMON.md and assigned Markdown documents. Free-form COMMON works
   without metadata. Optional COMMON YAML `goals` entries have `id`, `kind`
   (`goal`, `invariant`, `prohibition`) and `text`; that exact text must appear in
   the source document. Task front matter may name `goal_ids` and `acceptance_ids`.
   JSON manifests also accept `goals` with an explicit `source` input path.
2. Select **根本原因の再計画policy**, review the converted manifest and dry-run,
   then opt in and Start. `policy.replanning` defaults to `propose_only` for old
   manifests as well. `off` disables replanning. `auto_within_envelope` allows
   additions within the start-time edit envelope and validation authorization.
3. Inspect **個別レビュー / Goal Alignment**. Review v2 records four independent
   axes, referenced explicit IDs, the fixed requirement digest and evidence.
   A task need not finish the entire workflow goal. Prohibited assumptions,
   architectural contradictions and unknown required evidence cannot APPROVE.
4. An implementation bug returns to the existing implementation thread. A
   supported faulty premise queues **独立Codex再分析**, a fresh read-only Codex
   thread with tools disabled. Post-fix validation failures receive independent
   diagnosis before another repair; a failing test alone never authorizes a new
   design. Environment/auth/quota failures do not trigger design changes.
5. Inspect **新タスク生成 / 根本原因の再計画**: cause, evidence, proposed tasks,
   graph before/after, scope differences, policy, remaining limits and immutable
   decision digest. In propose-only mode, select **Owner approve new tasks** or
   **Owner reject new tasks**, with a rationale. Generic Resume is not approval.
   Approved proposals add nodes immediately; a paused workflow dispatches only
   after Resume. Changes to the source candidate/turn, plan, generation or fixed
   input invalidate the decision. Repeated approvals cannot add duplicates.

Unapproved edit paths always require an explicit graph decision, even in auto
mode. COMMON/rule-source edit scopes also require owner review. Model requests to
change goals, COMMON, permissions, mandatory checks, trust, gates, publish or
deploy remain proposals with `owner_changes`; the approve button is disabled.
These changes require owner-authored requirements/authorization and a newly
reviewed workflow. A model-generated string is never an authorization. The
original source and findings remain visible while other independent tasks run.

## Fixed requirements and review versions

New candidates use requirement bundle version 2. It distinguishes global goals,
invariants/prohibitions, task purpose/acceptance and base rules, and retains every
original source body and digest. Free-form goals are not transformed into invented
owner-approved acceptance conditions. Implementer, correction and review access
the same `.autopilot-inputs` bundle. Added tasks inherit COMMON, the source task's
assigned documents, explicit associations, contract, gates and accepted inputs.

`goal_review.version=2` is mandatory for these candidates, including every diff
partition and the overall review. An old review cannot satisfy that gate. Old
ledger data is retained; old requirement bundles are not silently rewritten.
Legacy manifest version 1 remains valid. If owner changes make old acceptance
inputs stale, re-submit and re-review rather than promoting an old result.
The input digest now covers explicit goal metadata too.

Each blocking finding needs a classification, reproduction, observed/expected
results, fixed requirement references, a counterexample and evidence references.
Supported classifications are IMPLEMENTATION_DEFECT, TASK_ASSUMPTION_FALSE,
TASK_CONTRACT_CONFLICT, MISSING_PREREQUISITE, ENVIRONMENT_FAILURE and INCONCLUSIVE.
Only the three premise/prerequisite classifications can reserve a replanner.
Controller evidence must contain a real tracked candidate snapshot range or a
failed validation record, or two conflicting exact fixed requirement bodies.
A separate replanner must confirm the cause and cite that registered evidence.
This is a structured evidence gate, not a mathematical proof of model analysis.

Required context over 1 MiB, unavailable evidence, invalid structured output,
exhausted snapshot/partition budgets and context-limit failures block explicitly.
Documents are never silently dropped to obtain approval.

## Dynamic graph transaction and source lifecycle

The hard initial limits are three tasks per proposal, one replanner reservation
per original node, depth one, and the existing 200 nodes per workflow. Reservations
also count interrupted/inconclusive analyses. New tasks share review slots and the
Codex provider semaphore; existing subscription-only quota checks remain active.

`ap_replans` retains fixed input, history, observations, model proposal and session.
`ap_graph_revisions` retains expected plan digest, generation, candidate SHA, exact
before/after graph, replacement relationships and decision target. One SQLite
transaction commits added nodes, edges, changes to unconsumed successors, the plan
and source replacement state. A unique source reservation and unique graph revision
prevent duplicate registration across ticks, connections and restarts.

Task proposal `relation` is `modify`, `replace` or `supplement`, describing its
intent toward the old deliverable. All dynamically generated deliverables must
retain the full original acceptance contract and mandatory checks. The original
execution is immutable: its node moves through `blocked_replan`,
`awaiting_replacement`, then `superseded` only after replacement leaves are
accepted. Its candidate, test evidence, findings, task outcome and original thread
are retained; it never becomes SUCCESS merely because a plan was generated.

Only pending/Draft/blocked-dependency successors with no task, generation,
candidate or consumed inputs may be rewired. Replacement leaves are ordinary
Autopilot nodes and must pass required validation and independent review before
successors consume their accepted SHA. Multiple code inputs still require an
explicit integration node. Invalid graphs and exhausted limits stop safely.
Interrupted running replanners remain blocked for inspection; they are not
blindly replayed. Queued reservations and saved proposals survive restart.

The owner decision endpoint is
`POST /api/autopilot/workflows/{id}/graph-revisions/{revision}/decision`, with
`owner_action:true`, `expected_target`, `action:approve|reject`, and nonempty `note`.
It reuses `ap_decisions` for versioned owner rationale and authorization audit.
Stop disables graph application and dispatch.

## Public isolated verification

[tests/fixtures/autopilot-goals](../../tests/fixtures/autopilot-goals) contains
public synthetic instructions. Prepare a disposable Git repository with committed
`capability.txt` containing `adapter-v2 only\n`, import these documents and use the
registered Python validation profile. The automated E2E uses these same signals
with fixture CLIs, real GUI APIs, TaskManager, Git worktrees, SQLite and bubblewrap
validation. It checks COMMON-only rejection despite passing individual checks,
partial goal acceptance, same-thread repair, independent root-cause analysis,
auto registration, owner scope decisions, replacement acceptance and dependency
handoff. Browser tests use a temporary localhost port and temporary data home.

```
python -m pytest tests/test_autopilot_goals.py tests/test_autopilot_documents.py tests/test_autopilot.py
CODEX_GUI_BROWSER=1 python -m pytest tests/test_autopilot_browser.py
CODEX_GUI_AUTOPILOT_CLI_SMOKE=1 python -m pytest tests/test_autopilot_cli_smoke.py
```

Fixture reviewers/replanners inspect exact documents, bundle/input digests,
observations and dependency inputs. They do not establish real model reasoning
quality or actual account/quota behavior. Installed CLI smoke uses a local scripted
endpoint with no real model usage. Real subscription model validation remains a
separate opt-in; no fixture PASS is reported as a real-model PASS.
