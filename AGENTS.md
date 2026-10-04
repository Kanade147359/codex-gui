
# AGENTS.md

## Scope

These instructions apply to the entire repository unless overridden by a more specific nested `AGENTS.md`.

Codex-GUI is a local, single-user control plane for running and managing Codex CLI tasks, Git worktrees, persistent Codex threads, dependencies, retries, and related development workflows.

Prefer the existing architecture and conventions over introducing new frameworks or abstractions.

---

## Live Development Safety

Codex-GUI may be modifying itself while the production GUI is running.

When working from a Codex-GUI task:

- Work only inside the assigned Git worktree.
- Do not directly modify the live/main worktree.
- Do not kill, restart, or reconfigure the production Codex-GUI process unless explicitly requested.
- Do not use the production HTTP port, normally `8765`, for development servers.
- Use a separate port such as `8766` for development instances.
- Never modify the production database from tests or development instances.
- Do not modify, delete, reset, or clean worktrees belonging to other running tasks.
- Do not merge into `main` automatically unless explicitly requested.

Production data may exist under paths such as:

`~/.local/share/codex-gui/`

Treat existing databases, logs, worktrees, and task state there as live user data.

Tests must use temporary databases, temporary data directories, and isolated repositories/worktrees.

---

## Core Invariants

Preserve these unless the task explicitly changes the architecture.

### Task isolation

One task normally owns:

- one Git worktree
- one task branch
- one Codex thread/session

Do not allow one task to silently modify another task's worktree.

### Codex session continuity

For additional instructions on an existing task:

- reuse the existing `codex_thread_id`
- resume the existing Codex thread
- preserve the existing worktree
- do not copy the previous conversation back into a new prompt

Start a new Codex session only when explicitly requested or when no recoverable thread exists.

### Speed

Standard is the normal/default execution mode.

Fast must be an explicit per-turn choice.

Do not infer that an entire thread must remain Fast because an earlier turn used Fast.

### Approval and sandbox

Prefer the existing auto-approval behavior.

Keep the normal workspace sandbox enabled.

Never introduce `--dangerously-bypass-approvals-and-sandbox` as a default or implicit fallback.

---

## Task Success Semantics

Process success and task success are different concepts.

An exit code of `0` does not necessarily mean the requested work was completed.

A task must not release dependent work when it is effectively:

- blocked
- waiting for required information
- incomplete
- awaiting review
- missing required artifacts
- unable to proceed safely

Do not invent missing requirements merely to mark a task successful.

When required information is absent or instructions are materially ambiguous, prefer a safe blocked/needs-input outcome.

Only a genuine successful task outcome should satisfy dependencies.

---

## Dependencies and Scheduling

Dependency execution must be race-safe and idempotent.

- Never start a dependent task before all required dependencies are successfully satisfied.
- Never start the same queued task twice.
- Prevent dependency cycles and self-dependencies.
- A dependency that is retrying is not yet failed.
- A dependency that reaches a final blocked/failed/stopped state must not silently release downstream tasks.
- Scheduled additional instructions targeting the same Codex thread must execute serially.

Use atomic state transitions or equivalent protection when scheduler loops can observe the same task concurrently.

---

## Automatic Recovery

Automatic retry is for unexpected or transient execution failures.

Reasonable retry cases include:

- unexpected Codex process termination
- transient subprocess or IPC failure
- temporary runtime failure

Do not automatically retry semantic failures such as:

- missing specifications
- ambiguous instructions
- missing required artifacts
- explicit user stop
- authentication failure
- quota exhaustion
- invalid configuration
- dependency failure

Respect retry limits and backoff.

When recovering an interrupted task:

- preserve the worktree
- preserve the Git state
- reuse the Codex thread when possible
- inspect existing work before continuing
- never `git reset --hard` or `git clean` automatically

Do not blindly resend the original task in a way that may duplicate already completed work.

---

## Git Safety

Before changing Git state, understand the current worktree.

Prefer:

- focused changes
- focused commits
- `git status --short`
- reviewing relevant diffs
- relevant tests before completion

Do not:

- force-push
- rewrite unrelated history
- reset unrelated user changes
- delete another task's branch/worktree
- merge to `main` without explicit instruction

SSH authentication may be provided through the parent Codex-GUI process. Preserve inherited SSH environment variables.

---

## Database and Migrations

Treat database compatibility as important.

Schema changes must be:

- migration-safe
- repeatable/idempotent where appropriate
- compatible with existing user data whenever practical

Never use the production database in tests.

When changing task states, scheduler behavior, dependencies, retry handling, or Codex thread metadata, add regression tests for the relevant state transitions.

---

## Testing

Run focused tests first.

Before declaring a substantial change complete, run the relevant broader test suite when practical.

For changes involving:

- scheduler behavior
- dependencies
- retries
- process lifecycle
- database migrations
- thread/session reuse

include regression tests for races, restart behavior, and duplicate execution where relevant.

Real Codex integration tests must remain opt-in and must not consume or alter production task state.

Do not make flaky tests pass by simply weakening assertions.

If a failure cannot be reproduced, preserve enough diagnostics to identify it next time.

---

## Context Efficiency

Keep model-visible context focused.

- Prefer `rg` over broad repository dumps.
- Read only relevant file ranges.
- Prefer focused test output over huge logs.
- Use `head`, `tail`, targeted filters, and concise summaries where appropriate.
- Do not repeatedly read unchanged large files.
- Do not dump entire generated files, databases, or logs unless necessary.
- Do not duplicate information already available in repository documentation.
- Do not paste `AGENTS.md` or architecture documentation into prompts when Codex can read them directly.

Large command output should be written to disk when useful and only the relevant portions inspected.

Do not optimize token usage at the expense of correctness.

---

## AGENTS.md

Keep this file concise.

Do not automatically rewrite or reorganize `AGENTS.md` merely for token optimization.

Efficiency tooling may warn about:

- excessive size
- duplicated instructions
- always-loaded information
- unnecessary references

but changes to project instructions should remain an explicit user decision.

Use nested `AGENTS.md` only when rules genuinely apply to a narrower subtree.

---

## Security and Remote Access

Codex-GUI is intended as a local/private tool.

Do not expose it publicly by default.

- Prefer localhost binding.
- Tailscale/private-network access may be supported.
- Do not enable public Internet exposure implicitly.
- Validate repository/worktree paths.
- Prevent path traversal outside authorized repositories.
- Treat destructive remote actions conservatively.

Do not silently fall back from subscription-based Codex usage to separately billed API usage.

---

## Implementation Style

Prefer simple solutions that fit the existing codebase.

Avoid:

- unnecessary framework additions
- duplicate implementations of existing task/session logic
- large abstractions for hypothetical future requirements
- hidden behavior that makes scheduler state difficult to inspect

Reuse the existing task manager, scheduler, Codex runner, Git manager, and persistence layers where appropriate.

When behavior is uncertain, inspect the installed Codex CLI and its actual output rather than guessing its command-line or JSON protocol.

---

## Completion

Before reporting completion:

1. Review the changed files.
2. Check Git status and relevant diff.
3. Run relevant tests.
4. Confirm no production data or live processes were modified unintentionally.
5. Report important limitations, unresolved issues, or behavior that could not be verified.

If the requested result cannot safely be completed, report the blocker instead of pretending the task succeeded.
