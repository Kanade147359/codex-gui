# Task C measured preview validation — 2026-10-11

Outcome: lifecycle and isolated builds work; **full Integrated Console operation
is blocked by the source Console's missing signed authentication**. This is a
development preview result, not Production Ready or a Release Gate pass.

## Sources and running previews

Corriedale baseline: `8498249670322f3cf94587675086bf2e20c4c976` from
`/home/kanade/workspace/corriedale-cloud`. Its checkout was read-only and remained
clean. For change scenarios, its source was exported into a **new task-owned
temporary Git repository** inside this tool worktree's ignored `.preview/`.
That repository's base commit is `5341bd50585b8ad01b057bfeae6a4e657bb7de69`;
this is a verification commit, not a Corriedale upstream commit.

| Field | UI | Integrated |
| --- | --- | --- |
| Preview ID | `9287883a1f09b504` | `1744625d84ddf919` |
| Branch | `preview-scenarios` | `preview-api-scenario` |
| Actual source commit | `5341bd50585b8ad01b057bfeae6a4e657bb7de69` | same |
| Dirty | yes, HTML title changed | yes, Node Identity JSON field added |
| Source SHA-256 | `e0bee3427a0f42f2214bef1e67783bdd7fbb46135fc648b53b9765515bfc506f` | `b8de8e63c8d852ee2c8044bbc18036361660da4d056ebb5dc18b5927d4583770` |
| Console URL | `http://127.0.0.1:47821` | `http://127.0.0.1:45005` |
| Build / frontend / backend | PASS / RUNNING / read-only MOCK | PASS / RUNNING / isolated Agent RUNNING |
| Health / Node Identity API | PASS / mock only | PASS / PASS |
| Console API access | mock PASS | BLOCKED, compatibility probe HTTP 401 |
| Retained at report time | yes | yes |

Read the password from the corresponding `.preview/ID/password` file in the
assigned Codex GUI task worktree. No passwords are committed or included in URLs.
Use `./preview.sh status ID` for current state: these are local ephemeral URLs,
not durable hosted services. The remaining prototype previews were stopped.
Artifacts, hashes, snapshots, logs and additional check results remain private
under the ignored state root for inspection.

## Scenario results

| Scenario | Result | Actual evidence |
| --- | --- | --- |
| 1. UI change | PASS | Dirty title `Task C dirty UI preview` in a fresh static build; real Chromium login, Dashboard and Nodes rendering |
| 2. API change | PASS for isolated API; Console management BLOCKED | Real compiled Agent's `/v1/node-identity` returns `preview_marker: task-c-isolated-api-change`; authenticated browser-context GET verified it. Nodes page shows the existing unsigned-request 401 error |
| 3. Multiple worktrees | PASS | Two real task-owned Git worktrees served different snapshots concurrently on different ports; cleanup of one preserved the other. Actual UI and Integrated previews also ran concurrently |
| 4. Failed build | PASS | Regression verifies a new failed ID has no URL/artifact success and leaves a prior preview healthy. Actual lint failures also produced failed IDs, never reused old builds |
| 5. Crash recovery / isolation | PASS | UI crash preserved another preview/unrelated process; Integrated supervisor SIGKILL reaped its private Backend; host loopback sentinel unreachable from Agent namespace |
| 6. Smartphone / Tailscale | NOT_RUN | No real smartphone supplied; no Serve or ACL changes authorized/performed. Remote runbook supplied |
| 7. Unauthorized access | PASS locally; remote device NOT_RUN | HTTP login required; unauthenticated same-origin POST → 401; wrong Host/Origin → 403; UI mock writes → 405; Agent rejects unsigned calls after preview login; remote-host cookies use Secure |
| 8. Cleanup | PASS locally; published route NOT_RUN | Per-ID process shutdown, Unix sockets removed, only that ID's directory removed. Remote-route marker blocks cleanup until operator removes only the owned route |

Automated Chromium screenshots were inspected by the agent. **User visual
approval is NOT_RUN**; browser launch success is not visual approval. The system
browser launcher was invoked through `preview open`; WSL emitted a vsock error,
so graphical desktop launch cannot be confirmed. Real headless Chromium did
successfully load and operate the previews.

## Checks

- Preview regression suite with real Chromium opt-in: **18 passed**.
  `CODEX_GUI_BROWSER=1 PLAYWRIGHT_BROWSERS_PATH=.preview/browsers .venv/bin/python -m pytest tests/test_preview.py -q`
- Changed Python files: Ruff formatting + `E9,F,I` lint PASS; shell syntax and
  Git whitespace checks PASS. No new application dependency/framework added.
- Broader Codex GUI suite: **748 passed, 3 failed, 12 skipped**. The three failures
  below also reproduced against an exported **unchanged HEAD baseline** with the
  same environment; assertions were preserved and unrelated code was not changed:
  `test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded`,
  `test_real_codex_tool_output_limit_reaches_the_model`,
  `test_tool_output_token_limit_only_lowers_the_cap`.
- Real Console: typecheck, lint, **25 tests**, production bundle build PASS.
  UI `verify` exercised checks → build → start → health → HTTP smoke → browser.
  Node 20.17 caused missing oxlint native bindings; a task-local Node **22.12.0**
  install resolved that environment prerequisite. Lint explicitly targets source
  roots so the parent's `.preview/` ignore rule cannot skip files. Reinstalling
  locked dependencies with the supported Node also fixed Integrated lint.
- Real dirty Agent: `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`,
  **226 library tests**, binary build and isolated health/API smoke PASS.
- Existing `tools/check-security-release --validate`: PASS, inventory only.
  **Production Release Gate / real OCI / optional WASM workload tests: NOT_RUN.**
- `preview smoke` on Integrated returns nonzero and records
  `console_api_access: BLOCKED` (HTTP 401), while document and backend identity
  checks pass. No authentication mode was lowered or signature injected.

## Boundaries and outstanding work

No main merge, Lab/VM deployment, production database/process change, Jenkins
change, host firewall change, Tailscale Serve change or Funnel exposure occurred.
Read-only Serve status before and after was `{}`. Builds write only their own
sandbox directory; runtime has no cluster network or containerd socket.

To complete real Console management verification, supply a Corriedale source
with its intended Console authentication flow, then exercise authorized read
and write operations against the isolated Backend using those credentials.
This tool forwards existing authorization and cannot replace that application
work. Smartphone HTTPS/ACL acceptance and published-route cleanup require
operator-authorized Serve configuration and a real device. Keep those tests
NOT_RUN until measured. Do not treat UI preview, inventory validation or the
successful identity query as a production release decision.
