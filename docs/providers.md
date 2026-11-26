# Execution providers

Codex (default) and Claude Code are ordinary task execution engines. They share
worktree ownership, queue/dependency claims, attempts, scheduled instructions,
retry decisions, messages/images, Completion Gate, evidence checks and shutdown.
HIRO is outside this integration.

## Architecture

- `app/providers.py`: `ExecutionProvider`, capabilities, normalized events, local
  discovery, CLI invocation/input, provider error classification and registry.
- `CodexProvider` preserves both exec and app-server transports. The existing
  app-server implementation is isolated in `app/codex_execution.py`; the public
  `CodexRunner` injection seam and old task/API fields remain available.
- `ClaudeCodeProvider` executes the official CLI directly with an argv array and
  separate stdin/stdout/stderr. No Anthropic API client is introduced.
- `TaskManager` owns shared lifecycle/completion decisions. A successful CLI
  process is insufficient: final structured semantic result and the existing
  evidence/approval rules decide whether dependencies are satisfied.
- Discovery invokes only local `--version`, `--help`, and authentication/catalog
  metadata commands. Missing Claude never prevents Codex startup/execution.

Claude runs `-p --output-format stream-json --verbose
--include-partial-messages --input-format stream-json --json-schema ...`.
Prompts and base64 images travel on stdin, including on `--resume UUID` turns.
Text deltas, tool calls/results, foreground subagent messages/task notifications
and usage are logged incrementally. Unknown events/raw malformed lines are
retained without scoring them as completion. Ambiguous duplicate keys/results,
wrong sessions, denied permissions and missing terminal envelopes fail closed.

Model IDs remain manually selectable (aliases or full IDs); Claude has no stable
model-list CLI. Effort/permission choices are parsed from installed help; manual
effort is also supported and the CLI validates model compatibility. Unsupported
compaction, live steering and Codex Fast options are hidden/rejected for Claude.
Codex-specific tool profiles/settings are never translated into pretend Claude
features. Provider concurrency caps are shared by ordinary work, reviews and
planners; the existing global cap still applies.

## Configuration and authentication

Dashboard Settings provides an executable path, default model/effort/subagents,
permission mode, concurrency cap, local authentication/version/availability and
billing route for each provider. `CLAUDE_BIN` supplies the initial Claude path.
Authentication stays with `claude auth login`; the GUI never stores or copies
ordinary-task credentials into its database. `CLAUDE_CONFIG_DIR` is preserved
when selecting an existing CLI login. No login or logout is performed by discovery.

Before Claude execution, both conditions are required:

1. Provider Settings explicitly selects Subscription and the owner confirms
   extra usage / purchased credits are disabled in their Claude account.
2. Fresh CLI `auth status` reports a first-party claude.ai subscription login.

The CLI does not expose an authoritative API for verifying that extra usage is
turned off. This confirmation is an owner assertion, clearly labeled in Settings;
a login alone does not provide it. Unknown billing, API-key/helper/cloud routes,
missing auth and unsupported versions stop before a model prompt is sent.
Metered execution is deliberately unavailable. Inherited API keys, routing flags,
cloud credentials, `NODE_OPTIONS` and other provider variables are excluded.
SSH agent variables remain available. Codex children do not inherit Claude/Anthropic
variables either; persisted logs redact common credential fields and known secret values. No API/provider fallback is implicit.
Quota stops pause the Claude queue until an explicit resume; an overage signal
also stops execution and the provider queue. Remaining quota stays Unknown.
CLI-reported cost is a reported API-equivalent figure, not a measured charge.

## Permissions and repository configuration

New Claude tasks require explicit repository trust. Immediate tasks verify billing/auth
before creating a worktree, and Claude worktree creation disables checkout hooks.
Deferred tasks repeat preflight before creating their worktree. The default permission mode
is `dontAsk`; choose `acceptEdits`, `plan`, or supported `auto` intentionally.
A permission denial ends the run for owner review. `bypassPermissions` and
`--dangerously-skip-permissions` are never permitted.

The command pins `--safe-mode`, empty `--setting-sources`, empty strict MCP config,
disabled hooks, plugins/skills/Chrome integration, subscription login and an
enabled CLI Bash sandbox with `failIfUnavailable=true`, no excluded commands, and
`allowUnsandboxedCommands=false`. Missing required
CLI flags or versions older than the verified 2.1.185 sandbox contract block execution. Repo/user hooks, MCP, plugins and CLAUDE.md are not
automatically loaded. Give necessary requirements in the task or fixed workflow
input documents; ordinary built-in foreground subagents can be enabled per task.
Background tasks are disabled to keep one process invocation/terminal result per
GUI turn. Isolated planner/reviewer/mentor roles have no tools or subagents;
requesting those capabilities is rejected.

Managed policies have higher precedence than CLI settings. Local managed files
that disable the sandbox, enable bypass, inject billing routes/key helpers, or
configure hooks are rejected. Server-managed organization policy is not fully
introspectable here; it remains administered by the CLI/organization. This is not
an independent VM boundary around all CLI internals. Real sandbox enforcement,
account limits and model compatibility require opt-in real CLI verification.

Processes are launched in their own groups. Stop, execution timeout and GUI
shutdown terminate the group, escalate to SIGKILL, drain/cancel pipes, and preserve
worktree/Git/session state. A surviving child cannot keep an exited leader's task
open indefinitely. The production GUI is never restarted by these mechanisms.

## Sessions and compatibility

Old tasks migrate to `provider=codex`, retaining `codex_thread_id`, history,
settings, attempts and dependencies. `provider_session_id` is the common identity;
API responses also expose `session_id`. Usage/attempt rows carry provider identity.
`provider_sessions` reserves `(provider, session_id)` for one task/worktree, even
across new sessions and restarts. Pending turns, task claims and scheduled FIFO
claims retain the existing atomic transitions. A missing/mismatched session
never silently becomes a new Claude session.

`POST /api/tasks/{id}/new-session` can explicitly change provider for an idle
ordinary task, with a new model and provider options. It clears the current
identity, retains earlier messages/usage, and starts a fresh CLI conversation;
it does not replay/copy the previous conversation. The controller appends an
explicit handoff containing the original task, latest instruction, current Git
state, previous semantic/evidence results and recent attempt identities. The new
provider is told to inspect existing work and avoid repeating completed actions. Workflow repairs retain their selected provider/session;
changing a workflow provider requires a revised plan/new task.

Migrations add columns and backfill ownership in one SQLite transaction. Before
upgrading an existing pre-provider database, a SQLite backup (including committed
WAL data) is saved as `codex-gui.db.pre-providers.bak`, mode 0600. A failed migration
rolls back; the backup is preserved. `Database.backup(destination)` produces a
consistent snapshot. Restore **offline into a separate data directory**, verify
it there, and only switch a stopped instance explicitly; there is no live restore
API. Never overwrite a running production database or copy only its main file
while WAL is active. Tests cover fresh/legacy/repeated/failed migration, backup
roundtrip and shutdown/restart session continuation.

## API examples

Existing requests without `provider` continue to create Codex tasks.

```json
{
  "repository": "/path/to/repo",
  "prompt": "Implement the requested change",
  "provider": "claude",
  "model": "sonnet",
  "reasoning_effort": "high",
  "allow_subagents": true,
  "provider_options": {
    "permission_mode": "acceptEdits",
    "trusted_repository": true,
    "timeout_seconds": 3600
  }
}
```

- `GET /api/providers`: local capabilities/status plus persisted defaults.
- `PUT /api/providers/{provider}/settings`: `{ "settings": { ... } }`; refuses
  unknown fields and changes while execution jobs are active.
- `POST /api/providers/{provider}/resume-queue`: explicit provider queue resume.
- Existing message/stop/retry/scheduled/completion APIs work for both providers.

## Autopilot and Research Mode

A node accepts `provider`, `review_provider`, `model`, `reasoning_effort`,
`allow_subagents`, and `provider_options`. Markdown supports `Provider: claude`
and front matter. `policy.roles` sets planner, implementer, reviewer and mentor
providers/models/efforts. The GUI exposes these choices; authored node/Markdown
settings retain precedence. Reviewers receive pinned snapshots, requirements,
prior findings and validation evidence in independent sessions. A quota fallback
requires `policy.allow_provider_fallback=true`; it does not impersonate the old
provider/session. Repairs go back to the implementation's original session.

```yaml
policy:
  roles:
    implementer:
      provider: claude
      model: sonnet
      provider_options: {trusted_repository: true, permission_mode: acceptEdits}
    reviewer: {provider: codex, model: ""}
    mentor: {provider: claude, model: ""}
    planner: {provider: codex, model: ""}
  allow_provider_fallback: false
```

Use `mode: research` (also selectable in the GUI) for report nodes with explicit
artifact validation. Both engines, role routing, dependencies and completion
checks apply. Independent implementation/research nodes use separate worktrees;
concurrent declared edit ownership cannot overlap. This is the existing report
workflow machinery, not a second scheduler or a new remote research service.

See [provider verification](provider-verification.md) for measured results,
NOT_RUN items and reproduction commands.

## Primary references

- [CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Non-interactive execution](https://code.claude.com/docs/en/headless)
- [Authentication](https://code.claude.com/docs/en/authentication)
- [Sandbox behavior](https://code.claude.com/docs/en/sandboxing)
- [Settings precedence](https://code.claude.com/docs/en/settings)

Installed metadata was checked against Claude Code 2.1.185. Metadata checks do
not count as a successful real execution E2E.
