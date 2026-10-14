# Autopilot (opt-in, unmerged)

Autopilot runs an explicit JSON/YAML DAG through submission, managed validation,
independent review, bounded repair, integration and pinned successor execution.
It uses the existing TaskManager scheduler and SQLite connection. Ordinary
`all_success` dependencies retain `completed && success && !completion_pending`.
It never deploys, restarts a service, changes billing or purchases/consumes credits.

Open `/autopilot`, paste a manifest, run **Dry-run**, inspect the fixed plan and
permissions, opt in, then **Start**. Multiple Markdown files or a ZIP can also be uploaded and edited before conversion.
See [Markdown workflows](document-workflows.md) for Draft planning, handoffs, owner
decisions, partitioned reviews and offline validation environments. Input bodies
are materialized inside each worker/snapshot and supplied to reviewers.
See [personal profile](personal-profile.json) for a minimal example. Change its
repository and required commands to your project. No empty code profile is allowed.

`extra_usage_disabled_confirmed` defaults false. Set it true only after the owner
confirms that **both** subscription accounts have extra usage/credits disabled.
This is owner confirmation, not automatically verified account state. API keys,
alternate endpoint/provider environment and incompatible OAuth authentication
block real review. A CLI may otherwise spend enabled extra usage without a quota
rejection. Autopilot does not change account settings.

Claude Code is preferred. A typed, supported-version included-quota event is the
only automatic reason to select an independent Codex review. A 429, arbitrary
assistant/tool message, authentication error, timeout or requested changes is
not a quota signal. Confirmed account/model/bucket availability is shared in the
ledger; elapsed reset time only admits one bounded recheck. No remaining-token
estimate is displayed. Codex review creates a fresh thread, not resume/fork of
the implementation conversation. Same-model separate-thread review is labeled
as such, not cross-model verification.

Both real adapters use an isolated CLI process with only staged subscription
credentials and trusted settings, no inherited SSH agent or API-key environment.
Claude uses `--safe-mode`, empty tools/settings sources and strict empty MCP
configuration, not `--bare`. Codex disables shell, plugins and subagents, uses a
new read-only thread and structured `turn/start`. Fixed requirements and partitioned base/head diffs are supplied as data. No candidate
hooks or model tools run. Large diffs require every partition and an overall verdict;
related snapshot code can be requested through a bounded structured read protocol.
The CLI process needs its OAuth credentials; these are not supplied to model tools
or prompts. Real model/tool enforcement remains subject to the recorded smoke status.

Validation uses bubblewrap with an allowlisted runtime filesystem, no GUI data,
home directory, network, SSH socket or other worktree. Source is read-only; scratch,
CARGO_TARGET_DIR and explicitly registered empty build-output directories are
writable. Install dependencies before the workflow. Commands use argv, fixed
timeouts (1–3600 seconds), controller-produced exit status, digests and bounded
redacted logs. Validation commands are arbitrary code: no unsandboxed fallback.
The managed validator cap does not constrain builds independently started by an
implementation agent; v1 does not claim OS/cgroup enforcement of those processes.

Graph and task detail show fixed inputs, candidates, validation, review provider,
fallback reason, findings, repair budget, acceptance and operation history.
“AI review accepted”, manual override and GitHub approval are different actions.
Old candidates and incomplete attempts stay in history. Normal implementation
success is not granted until the controller gate commits acceptance atomically.

Explicit new instructions for an unconsumed/leaf task resume its existing thread,
reserve a new turn and invalidate prior review generations. Scheduled instructions
use the same atomic reservation and wait for independent acceptance before the next
instruction. Owner instructions are audited as input; they do not clear manual gates
or silently update already-consumed downstream inputs. An accepted task may start a
new leaf turn; only genuine prior success resets its automatic repair budget.

Pause stops new dispatch. Already claimed read-only validation/review may finish;
their results are saved and acceptance waits for Resume. A claimed integration
operation keeps its durable metadata. Stop increments run generation, cancels only
workflow-owned jobs/turns and disables retry, fallback, merge and successors.
Stopped workflows cannot automatically resume; a new reviewed plan is required.
Worktrees and logs remain available.

On restart, known completed results are reconciled. A recorded completed merge is
checked against the worktree branch, fixed head, clean index and every pinned input
before submission continues, without issuing another merge. An external operation with
unknown outcome or a still-live CLI enters `reconciling`, preserving PID identity,
Git ref, scope and logs. It is not blindly launched a second time. `Reconcile`
performs inspection; uncertain operations require an owner-reviewed recovery plan.
This does not promise exactly-once external CLI consumption. Accepted candidates,
repair reservation and downstream release are idempotent in SQLite.

The default host caps are implementation 3, review 1, managed validation 1 and
integration 1. Workflow policy may reduce these; host Settings may change the
caps (`CODEX_GUI_AUTOPILOT_IMPLEMENTATION_SLOTS`, `REVIEW_SLOTS`, `VALIDATOR_SLOTS`,
`INTEGRATION_SLOTS`, each with the `CODEX_GUI_AUTOPILOT_` prefix). Review/fix/integration
precede new work and old queued tasks age into priority.
Codex fallback uses the existing `_slots` provider semaphore. No second scheduler
or double-held implementation semaphore is added.

Each integration node owns its worktree and workflow branch under
`autopilot/<workflow-id>/integration/<node-id>`. It merges explicitly
pinned accepted SHA inputs, skips commits already included, records order and
start/end refs, and always validates/reviews the combined result. Conflicts use
the dedicated integration Codex task, then submit a new full candidate. Successors
start at the accepted integration SHA, not a newly resolved moving base branch.
The optional merge-then-implement mode always runs integration implementation after
merging, including conflict-free merges. Report artifacts remain a different kind from code inputs. Manual/owner gates
remain blocking for that slice; independent slices continue.

`publish_main` defaults false. If explicitly enabled, the final node must be an
accepted integration node, main must still match the pinned initial target, and
main must have no live checkout. Publication uses compare-and-swap update-ref,
never force push or checkout mutation. A changed target/live checkout stops with
the candidate retained. Deploy/restart is always separate.

## Existing Needs Review

1. Load **Dry-run分類** or `GET /api/autopilot/backlog`. Categories separate manual
   requirements, missing structured results, validation deficits and unreviewed tasks.
2. Inspect and commit existing dirty worktrees before opt-in; the controller must not
   accidentally commit unrelated owner edits. Preserve each existing completion contract.
3. `POST /api/autopilot/backlog/dry-run` with selected `tasks` entries containing
   `task_id`, `paths`, and a registered `validation` profile. This returns one manifest
   and preview. It does not modify tasks. Mixed repositories, cycles, conflicts,
   missing validators and duplicate enrollment are rejected before registration.
4. Paste/review the returned manifest, opt in and Start. A missing result gets a
   bounded correction turn in its original thread. Validation is rerun by the
   controller. Manual gates are retained and cannot be cleared by AI review.

## Development verification

```
python -m venv /tmp/codex-gui-autopilot-venv
/tmp/codex-gui-autopilot-venv/bin/pip install -r requirements-dev.txt
/tmp/codex-gui-autopilot-venv/bin/pytest -q tests/test_autopilot.py
```

Optional isolated checks (no real model calls):

```
CODEX_GUI_AUTOPILOT_CLI_SMOKE=1 pytest -q tests/test_autopilot_cli_smoke.py
CODEX_GUI_AUTOPILOT_AUTH_SMOKE=1 pytest -q tests/test_autopilot_auth_smoke.py
CODEX_GUI_BROWSER=1 pytest -q tests/test_autopilot_browser.py
```

The auth smoke stages only OAuth credentials in a private temporary home and reads
CLI auth metadata; it does not inspect limits or start model turns. Credentials are
read-only in review processes, so a required refresh may stop for owner login instead
of modifying the original account state. Real subscription review requires the owner
confirmation described above and is a separate operational acceptance step.

All new tests use temporary databases/repositories, fake implementation CLI,
synthetic review CLI and real isolated managed validators. The A/B/C→I1→D test
includes A repair, B quota fallback, restart, accepted integration and D reading
the integrated code. There are no manual Send/Approve/merge actions after Start.
Fake quota events are fixtures, not exhausted real subscriptions. See the final
verification record for coverage, compatibility and outstanding real-provider smoke.

Current document-workflow acceptance: [verification](document-workflow-verification.md) and [fixed-SHA results](document-workflow-verification.json). Earlier bootstrap records remain historical.
