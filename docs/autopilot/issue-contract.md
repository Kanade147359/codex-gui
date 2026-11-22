# P0-A: GitHub Issue Task Contract / Context Collector

This is a read-only preflight, not an Issue runner. It follows SWEBOK requirements
traceability (KA 1), architecture (KA 2), verification (KA 5) and
configuration management (KA 8).

## Usage

1. Install and authenticate GitHub CLI with gh auth login.
2. Open /autopilot, choose an existing local Git root and fixed base ref.
3. Enter https://github.com/owner/repo/issues/N and generate a contract.
4. Inspect the blockers, extracted acceptance items and referenced context.
5. Provide owner-approved edit paths and an explicit validation profile.
6. Generate again, review the manifest and use the existing Dry-run / opt-in
   workflow. Nothing starts until the owner explicitly opts in.

POST /api/autopilot/issues/preview receives a JSON object with:

- repository: absolute local Git root.
- issue_url: exact canonical GitHub Issue URL.
- base_ref: pinned branch or commit reference (default main).
- paths: owner-authorized edit path array (optional, default empty).
- validation: owner-authorized Autopilot validation profile (optional).

When paths or validation commands are missing, the response contains a non-ready,
draft node with explicit blockers rather than a guessed executable task.

## Trust boundary

- The authenticated gh CLI reads only the selected GitHub Issue.
- The Issue must match the local repository's origin remote.
- Title, body and update timestamp are frozen into issue.md in the generated
  workflow inputs; the response includes the Issue body digest.
- Referenced Markdown is read from the fixed base commit with provenance and
  digests; code paths are references, not imported source code.
- Existing Autopilot continues to freeze applicable AGENTS.md / CLAUDE.md.
- Issue text never sets permissions, validation commands, edit ownership,
  billing options, or Git publication.
- Readiness is STRUCTURAL only. Completeness and correctness of test oracles
  require separate inspection.
- The generated policy defaults to automatic_acceptance=false and
  publish_main=false. Registration still requires the normal dry-run and opt-in.
- No live worktree, task, workflow or production database is modified by preview.

## Limits

Issue body: 128 KiB. Referenced files: 8. Frozen Markdown context file:
64 KiB each. Issue changes do not update already pinned workflow inputs;
regenerate after a source revision to incorporate changes.

For ambiguous requirements, update the Issue's acceptance criteria rather than
inventing expected outcomes. No LLM or separately billed API is invoked.
