# Autopilot (opt-in, unmerged)

Autopilot runs an explicit JSON/YAML DAG through submission, managed validation,
independent review, bounded repair, integration and pinned successor execution.
It uses the existing TaskManager scheduler and SQLite connection. Ordinary
`all_success` dependencies retain `completed && success && !completion_pending`.
It never deploys, restarts a service, changes billing or purchases/consumes credits.

Open `/autopilot`, paste a manifest, run **Dry-run**, inspect the fixed plan and
permissions, opt in, then **Start**. Input Markdown belongs in the manifest's
`inputs` map; nodes reference it by `input_files`. The controller writes a fixed
read-only input bundle, rather than repeating documents in each prompt.
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
new read-only thread and structured `turn/start`. Source and full base/head diff
are supplied as data. No candidate hooks or model tools run. The initial reviewer
supports source bundles up to 1 MiB; larger scopes stop explicitly for partitioning.
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

Pause stops new dispatch. Already claimed read-only validation/review may finish;
their results are saved and acceptance waits for Resume. A claimed integration
operation keeps its durable metadata. Stop increments run generation, cancels only
workflow-owned jobs/turns and disables retry, fallback, merge and successors.
Stopped workflows cannot automatically resume; a new reviewed plan is required.
Worktrees and logs remain available.

On restart, known completed results are reconciled. An external operation with
unknown outcome or a still-live CLI enters `reconciling`, preserving PID identity,
Git ref, scope and logs. It is not blindly launched a second time. `Reconcile`
performs inspection; uncertain operations require an owner-reviewed recovery plan.
This does not promise exactly-once external CLI consumption. Accepted candidates,
repair reservation and downstream release are idempotent in SQLite.

The default host caps are implementation 3, review 1, managed validation 1 and
integration 1. Workflow policy may reduce these; host Settings may change the
caps. Review/fix/integration precede new work and old queued tasks age into priority.
Codex fallback uses the existing `_slots` provider semaphore. No second scheduler
or double-held implementation semaphore is added.

Each integration node owns its worktree and task branch. It merges explicitly
pinned accepted SHA inputs, skips commits already included, records order and
start/end refs, and always validates/reviews the combined result. Conflicts use
the dedicated integration Codex task, then submit a new full candidate. Successors
start at the accepted integration SHA, not a newly resolved moving base branch.
Report artifacts remain a different kind from code inputs. Manual/owner gates
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

All new tests use temporary databases/repositories, fake implementation CLI,
synthetic review CLI and real isolated managed validators. The A/B/C→I1→D test
includes A repair, B quota fallback, restart, accepted integration and D reading
the integrated code. There are no manual Send/Approve/merge actions after Start.
Fake quota events are fixtures, not exhausted real subscriptions. See the final
verification record for coverage, compatibility and outstanding real-provider smoke.
