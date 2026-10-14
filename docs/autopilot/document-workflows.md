# Markdown workflows

This extension uses the existing Autopilot controller, TaskManager scheduler,
SQLite ledger, isolated worktrees and independent review providers. It neither
runs the importer to bootstrap development nor changes an existing GUI instance.

## Import and Start

Open `/autopilot`, select multiple UTF-8 Markdown files or a ZIP and select
**取込・一覧編集**. Import creates no workflow, tasks, worktrees or model sessions.
`COMMON.md` is the shared document. Each other Markdown file is an assigned task.
An optional YAML front matter block supplies metadata; Markdown bodies stay in
the input bundle and need never be copied into JSON. The editable list includes
ID, dependencies, edit ownership, required validation, manual gates, Draft status,
planning envelope and integration mode. Edit the document text directly as needed.
Select the repository/base and explicitly authorize controller commits if desired,
then **manifestへ変換・Dry-run**, inspect, opt in and **Start**. The existing JSON/YAML
manifest editor remains available. A changed manifest requires another dry-run.

The public runnable example is [tests/fixtures/autopilot-documents](../../tests/fixtures/autopilot-documents).
Upload all seven Markdown files together into an isolated test repository.
Its flow is 00 → A/B/C → I1 → D; I1 also depends on 00 for the planning handoff.
Its commands deliberately check sandbox-visible handoffs. The synthetic model
fixtures enforce the COMMON-only condition, repair A in its original thread,
exercise B's Claude quota fallback, and inspect actual review scope and documents.
These fixtures are not evidence of real model quality or subscription billing.

Supported node metadata extends v1 with:

- `draft: true`: retain unresolved edit paths/validation; never dispatch this node.
- `envelope.paths` and `envelope.validation_profiles`: preauthorized choices for
  an accepted planner to concretize. Missing choices require an owner decision.
- `plan_artifact`: a required, regular JSON artifact containing `{"nodes":[...]}`.
  Revisions can set paths, validation, prompt, input references, handoffs and related
  review files on existing Draft nodes. Graph/mode/permission changes are rejected.
  Ownership expansion, unregistered validation or changed approval contracts wait
  for explicit owner approval with before/after specifications shown in the graph.
- `handoff`: regular text artifacts to pass to direct successors, in addition to
  required validation artifacts. Each is pinned to accepted candidate ID, full SHA,
  relative path and SHA-256 of the exact UTF-8 bytes.
- `integration_mode: merge-only` (legacy default) or `merge-then-implement`.
  The latter always executes Codex after the merge, including conflict-free merges.
  Conflict resolution is followed by another implementation turn in the same thread.
- `review_files`: related tracked snapshot files to read with bounded line ranges.

`POST /api/autopilot/import` accepts multipart `files`; `/import/convert` validates
an edited import plus repository/profile/permissions and returns a manifest and
preview. Uploads are bounded to 200 entries, 256 KiB per document, 4 MiB total,
and an expansion ratio of 100 (small files have 4 KiB allowance). Traversal,
absolute/drive paths, duplicate names, archive symlinks/special files, encrypted
archives and non-Markdown entries are rejected. ZIP contents are never extracted
onto the host filesystem. Import and conversion do not execute candidate code.

## Fixed requirements and handoffs

The controller freezes owner documents and applicable base-version AGENTS/CLAUDE
rules, and materializes them under `.autopilot-inputs/` inside the implementation
worktree and fixed review/validator snapshot. `requirements.json` identifies every
body, origin and digest. Implementers read those files in their existing sandbox;
reviewers receive the same bodies as review data. Additional owner instructions
are also fixed in candidate acceptance input and checked against the live turn.
Candidate edits to repository rules cannot waive the captured requirements.

Controller input files do not enter Git commits. Missing, changed, extra or symlinked
bundle files invalidate submission/acceptance. A changed instruction turn revokes
old review generations. Applied plan revisions are immutable audit records;
executed/consumed nodes cannot be rewritten by a later planning artifact.

## Review context

Reviews no longer concatenate all tracked files. The controller streams the fixed
base/head diff into bounded segments, includes every changed file/segment, selected
related code and all fixed requirements. An unchanged >1 MiB document has no effect
unless explicitly required. Each segment is independently reviewed; multiple
segments also require an overall review of all results and cross-file behavior.
No overall APPROVE can erase a negative/incomplete partition or unresolved finding.
Completed partition digests, paths, sessions and verdicts are recorded in the attempt.
Quota interruption retains findings already produced before switching providers.

The structured `requested_reads` protocol can request tracked snapshot paths and
line ranges (240 lines/read, 16 reads, four continuations). Controller reads have
path/symlink checks and bounded bytes. Reviewers get no shell/file tools, GUI APIs,
DB, credential files or external network tools. Missing context stays unverified;
read/partition budgets stop explicitly, never silently truncate into approval.
A task exceeding 256 diff segments must be split explicitly. Large required documents
still consume model context; runtime context-limit failures remain bounded failures.

## Owner decisions

A normal workflow Resume never grants owner approval. For a `blocked_decision` node,
the graph exposes its exact target digest and owner rationale, **Owner approve**,
**Owner reject**, and **Owner resume**. Approval is recorded while the node stays
blocked; explicit resume requires approval for that same target. A changed contract,
candidate, run generation or decision target invalidates the old decision. Rejection
keeps the slice blocked. Stop disables these actions. `manual_gates` authorize a
design before implementation. `contract.manual_approval` instead requires a separate
owner approval of the exact completed candidate, after required validation and AI
APPROVE. A design approval never satisfies that final candidate gate. Disabling
`automatic_acceptance` also requires this explicit candidate approval. Once all gates
are satisfied, waiting successors resume with the accepted SHA. AI review remains
mandatory even after owner approval; reviewer-requested human decisions
are returned to a fresh independent review rather than promoted directly to success.

API: `POST /api/autopilot/workflows/{id}/nodes/{name}/decision` with
`owner_action: true`, `expected_target`, `action`, and nonempty `note`.
Plan expansion decisions use `/workflows/{id}/revisions/{revision}/decision` with
`owner_action`, `expected_target`, and `approve`/`reject`. The graph shows complete
proposed specifications, permission differences, source artifact and history.

## Offline validation environments

Profiles may add `environment` with `toolchains` (argv probes and optional version
substring), `mounts` (registered source directories → `/opt/validation/<name>`),
allowlisted `env` values and `offline: true`. Example:

```json
{
  "commands": [["python3", "-c", "import sys; assert sys.version_info >= (3, 10)"]],
  "outputs": ["build"],
  "environment": {
    "offline": true,
    "toolchains": [{"command": ["python3", "--version"], "contains": "Python 3"}],
    "mounts": [{"source": "/opt/prepared/runtime-cache", "target": "/opt/validation/cache"}],
    "env": {"PYTHONPATH": "/opt/validation/cache"}
  }
}
```

Prepare Rust toolchains, Node packages or Python dependencies before Start. Mount
only narrow prepared directories, never a home root, GUI state or credentials.
Registered mount trees with symlinks, sockets or known credential files are rejected;
prepare Python environments with copied executables if necessary. For tools that
need writable caches, copy the registered offline cache into scratch using a required
validation command. Source remains read-only and declared output directories get
private temporary mounts. Cargo/npm offline flags are enforced by the environment.
The controller probes executables, registered toolchains and dependencies before
implementation/repair and again before formal validation. Infrastructure absence
blocks without consuming a fix round; owner Resume retries after correction.
There is no network/sandbox bypass or automatic installation/billing change.
