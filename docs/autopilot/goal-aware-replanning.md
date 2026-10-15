# Goal-aware review and bounded root-cause replanning

This extends [document workflows](document-workflows.md). It uses the same TaskManager,
Scheduler, SQLite ledger, frozen input bundle, managed validator, independent review,
owner decisions and integration path. Importing documents does not execute them.

## Register the requirements

Upload COMMON.md and task Markdown at `/autopilot`, inspect/edit the imported metadata,
convert to a manifest, dry-run, then Start. JSON/YAML manifests remain supported.
COMMON prose remains binding even without IDs. No model-generated interpretation is
recorded as an owner-approved goal. Fixed original bodies, base rules, additional owner
instructions and accepted handoffs are available to implementations, repairs, reviewers
and the replanner; candidate changes cannot waive those originals.

Optional explicit COMMON front matter:

```yaml
goals:
  - id: LOCAL
    kind: prohibition  # goal, invariant, prohibition, architecture
    text: Do not send input outside the local system
    source: COMMON.md
    quote: No remote access
```

Task metadata can declare `goal_ids`, `acceptance_conditions` and `assumptions`:

```yaml
goal_ids: [LOCAL]
acceptance_conditions:
  - id: TRANSFORM
    text: Transform the accepted input locally
    goal_ids: [LOCAL]
assumptions:
  - id: FORMAT_JSON
    text: Input is JSON
```

These IDs are owner-provided declarations, not facts inferred by the controller. A goal
source quote must occur in the fixed input. Assumptions are distinguished from goals and
acceptance: a verified counterexample can invalidate an assumption without relaxing the
goal or acceptance. Free-form premise corrections require an owner interpretation.

## Review v2

New workflow manifests require `goal_review_version: 2`. Each strict result includes a
versioned `goal_assessment` bound to the requirements and goal-index digests. Four axes
record MATCH / VIOLATION / UNKNOWN, goal and acceptance IDs, fixed source references and
evidence: task correctness, goal alignment, architectural consistency, evidence
completeness. Global axes must account for all declared goals and applicable COMMON/rules.
Task correctness must account for every explicit acceptance condition.

A task implementing one consistent slice may pass while the overall goal remains partly
unfinished. A contradiction, missing evidence, blocking finding or incomplete partition
prevents acceptance even when local tests pass. Every diff partition and the final
whole-scope review remain mandatory. An overall APPROVE cannot erase a partition's
negative goal assessment. A v1 result cannot satisfy a v2 candidate. Persisted legacy
workflows retain their prior contract; they are not silently opted into new requirements.

Review diagnoses distinguish IMPLEMENTATION_DEFECT, TASK_ASSUMPTION_FALSE,
TASK_CONTRACT_CONFLICT, MISSING_PREREQUISITE, ENVIRONMENT_FAILURE and INCONCLUSIVE.
Clean reviews use NO_ISSUE with no adverse finding IDs.
Adverse premise diagnoses require reproduction, observed/expected results, rationale,
finding IDs and exact evidence anchors. The controller verifies source quotes against
tracked fixed snapshot files, requirement quotes against the frozen originals, and
validation references against controller-run records. Test failure alone, README claims,
authentication problems and quota failures never authorize a graph change.
Source anchors may use `path#L301-L320` for counterexamples beyond the first page; the
same bounded, tracked-snapshot reader verifies those exact lines.

## Choose a replanning policy before Start

```yaml
policy:
  replanning:
    mode: propose_only  # default; off or auto_within_envelope also supported
    max_tasks: 3
    max_per_node: 1
    max_depth: 1
```

`off` keeps bounded original-thread repair. `propose_only` records independent Codex
proposals and waits for an explicit owner decision. `auto_within_envelope` can add tasks
within the original edit paths/envelope, registered validation profiles, permissions,
manual gates, explicit acceptance and global goals. Budgets may be lowered; their current
supported maxima are 3 / 1 / 1. Historical nodes count toward the existing 200-node limit.
Generated nodes cannot recursively replan. A model cannot change policy or these limits.

A clear, anchored initial review diagnosis can trigger analysis immediately. Post-repair
validation failure and repeated/no-progress failures prioritize a bounded independent
diagnosis; they do not presume a design error. If that diagnosis identifies an ordinary
bug, the original Codex thread receives the original failure fingerprint/findings and
continues within its unchanged fix/no-progress budget. Infrastructure failures wait.

The replanner is a fresh independent Codex thread, never a resume/fork of implementation
or review. Its immutable packet includes original requirements, full plan/dependencies,
candidate scope, managed validation, findings, relevant fix history, accepted inputs and
authority. Diff partitions and bounded related source are included. Additional tracked
snapshot ranges use `requested_reads` (240 lines each, 16 total, at most four continuations).
Missing reads require INCONCLUSIVE; an exhausted input/read budget stops explicitly.
There is no arbitrary shell, model tool network, GUI DB, approval API, or writable Git
access. The CLI retains only its isolated model connection. No home-wide mount is added.

Claude remains first choice for ordinary review. Only confirmed included-quota exhaustion
permits independent Codex fallback. Replanning itself uses Codex and shares the existing
Codex execution semaphore and review resource cap. Quota waits release CPU/provider slots;
only a bounded single availability probe may retry. Time passing is not an availability
signal. No billing settings, credits, alternate API route or production state are changed.

## Inspect and decide

The graph shows goal links, individual/goal assessments, diagnosis evidence, fix history,
proposed tasks, before/after dependencies, remaining limits and owner-required changes.
New-task generation is displayed separately from original-thread repair.

For `awaiting_owner`, review the entire revision and enter a reason, then use
**Owner approve root-cause plan** or **Owner reject root-cause plan**. Generic Resume and
AI APPROVE do not authorize it. API:

```text
POST /api/autopilot/workflows/{workflow}/graph-revisions/{revision}/decision
{"owner_action":true,"expected_target":"<decision_target>",
 "action":"approve","note":"Reviewed this exact before/after revision"}
```

An explicit owner decision can authorize a proposed edit-path/profile expansion within an
otherwise valid new graph. Goal/COMMON changes, permission/trust/design changes, manual
gate removal, acceptance removal or mandatory test weakening are hard blocks: preserve
the proposal and create a separately owner-authored, reviewed workflow/contract. The
decision endpoint cannot waive those constraints or fabricate new global requirements.

The target binds run generation, original candidate/generation, fixed requirements and
the complete before/after plan. Changed source SHA, dirty source, a new turn, stale plan,
consumed successor input, duplicate decision or Stop refuses application. A database
transaction commits all new formal nodes, edges, graph revision and replacement lineage,
or none. A restart/duplicate tick does not duplicate tasks. Unknown external CLI outcomes
are reconciled by process identity and retained for inspection, never blindly relaunched.

The original task remains unsuccessful and retains its PR/candidates/tests/findings.
Its state progresses through awaiting replacement to superseded only after all new nodes
are independently accepted. Only unstarted, unconsumed successors may be rewired to the
replacement terminal, and they wait for its accepted SHA. Generated tasks inherit COMMON,
original instructions, goals, authority and fixed lineage; a proposal's added acceptance
is labelled as proposed, not owner-approved. The unaccepted original candidate is an
explicit seed, reviewed again from its original base; inherited changes must fit each
new task's ownership. Integration nodes continue merge-only or merge-then-implement as
configured. Stop cancels only workflow-owned work and invalidates late results/retries.

## Isolated verification

Public Markdown fixtures: `tests/fixtures/autopilot-goals/`. They intentionally assume JSON
while the temporary repository supplies CSV. The fake implementation first returns a
constant, is independently reviewed, repairs in the same thread to a JSON parser, then
fails the real managed validator. A separate replanner verifies the fixed CSV counterexample,
proposes a bounded replacement, and the real controller/SQLite/Git/TaskManager run that
replacement and successor after a GUI restart. No manual Send/Approve/merge is used in
the automatic case. Separate tests cover COMMON-only rejection despite passing tests,
partial-goal acceptance, owner expansion, stale/consumed inputs, Stop, quota and attacks.

```sh
pytest -q tests/test_goal_replanning.py tests/test_autopilot.py \
  tests/test_autopilot_api.py tests/test_autopilot_documents.py
CODEX_GUI_BROWSER=1 pytest -q tests/test_goal_replanning_browser.py
CODEX_GUI_AUTOPILOT_CLI_SMOKE=1 pytest -q tests/test_autopilot_cli_smoke.py
```

Fake providers inspect the actual received bodies, scope, source views, plan, evidence and
authority. Installed-CLI local scripted-endpoint smoke verifies protocol and isolation;
it does not verify model quality, subscription spending or real quota exhaustion. Real
model calls remain explicit opt-in. See the fixed-SHA verification record for measured,
fixture and NOT_RUN results. This implementation is unmerged and not deployed.
