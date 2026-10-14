# S0 connection contract

Baseline: `405d722daab33e8a5ff4413373a9f384f80b47b9`. Assigned worktree was clean.
No Autopilot module or matching unmerged local commit existed at inspection.
Other task branches/worktrees were inspected by ref name only and left untouched.
No worker slot was available, so one owner edits and integrates all shared code.

- `app/autopilot/ledger.py`: additive SQLite migration, fixed candidate history,
  review attempts, managed evidence, acceptance, provider availability and operation journal.
- `policy.py`: JSON/YAML manifest, fixed profile, edit ownership, DAG and explicit permissions.
- `controller.py`: state machine called by the existing TaskManager tick. `task_outcome`
  remains needs_review until the atomic acceptance gate, including mechanically merged work.
- `providers.py`: review-only Claude/Codex adapters; immutable data bundle, strict final
  schema, typed quota classification, separate sessions and cancellation.
- `git.py` / `runner.py`: fixed-SHA standalone snapshots, managed integrations,
  allowlisted bubblewrap validation with scratch/output separation.
- Shared edits: database migration entry, TaskManager submit/fix/dispatch connection,
  existing AppServerClient isolated environment hooks, route composition and UI links.
- No changes to AGENTS.md, production state, existing all_success semantics, billing
  settings, live server configuration or main checkout. Legacy completion schema is retained.

API identities are controller-generated. Scope is full base/head/tree SHA + binary
diff digest, pinned policy/contract/input/profile digests, candidate and run generation.
Acceptance requires matching managed validation, a terminal independent APPROVE,
coverage of all changed paths, no unresolved findings/unverified scope, implementation
SUCCESS, unchanged task turn/contract, permitted automatic policy and no manual gate.
