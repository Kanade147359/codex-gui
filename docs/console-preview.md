# Console Preview from Codex GUI

This repository supplies `./preview.sh`, a standalone Linux CLI for previewing
**Corriedale Cloud**, without a Codex GUI API, merge, deployment or CI change.
Run it from this task worktree and pass the target Corriedale Git worktree root.
It does not preview the unauthenticated Codex GUI itself.

## Phase 0 findings (2026-10-11)

The assigned repository is `Kanade147359/codex-gui`. Corriedale was found at
`/home/kanade/workspace/corriedale-cloud`, SHA
`8498249670322f3cf94587675086bf2e20c4c976`. Other tasks' worktrees were inspected
only for documented interfaces; none were edited or deployed.

| Area | Observed source / boundary |
| --- | --- |
| Console build | `apps/console/package.json`: `npm ci`, `npm run build` (TypeScript + Vite), static `dist/` |
| Development proxy | `vite.config.ts`: `/v1/*` → `CORRIEDALE_API_PROXY_TARGET`, default localhost:7400; production Console uses same-origin API |
| Agent | `cargo build --locked --bin corriedale`; `corriedale agent start --config FILE`; explicit listen, advertise, data directory and containerd socket |
| API / authentication | `src/agent/peer_auth.rs`, `src/config.rs`: `request_auth = "require"`, fresh operator attestation and node key; unsigned management requests are refused |
| Console authentication gap | This SHA's `src/api/client.ts` sends unsigned requests and has no login. The preview adds a separate password gate and forwards existing Agent credentials; it cannot create missing Console authorization |
| TLS | ADR-0021 / ADR-0035 describe an operator-managed TLS terminator and protected management listener. The live deployment's actual certificate/proxy configuration was not accessed |
| Worktrees | GUI creates task branches/worktrees via `app/git_manager.py`; preview accepts a path and copies tracked plus nonignored untracked files, including dirty changes |
| Jenkins | Main checkout has no Jenkinsfile. `fix/jenkins-security-boundaries` has a pinned pipeline and `ci/jenkins/README.md`: nonroot, network-none build containers; live Jenkins was not changed or queried |
| Existing Console tests | `npm run typecheck`, `npm run lint`, `npm test`: Node test runner / React rendering with mocked APIs. No Playwright Console E2E configuration at the inspected SHA |
| Command / browser | CLI from the task terminal or Codex shell tools; Python `webbrowser.open`; URL-only fallback when no browser is configured |
| 9VM Lab | `lab/kvm`: three routers + six nodes with manifest-owned VM/network/storage/firewall resources. Preview never invokes `make lab-*`, `virsh`, `ctr`, host firewall or deployment tools |
| Tailscale | Installed 1.102.4; read-only `tailscale serve status --json` returned `{}`. No Serve/Funnel/ACL/firewall changes performed |

## Commands

Requires Python 3.10+, Git, npm with a Node version supported by the target
lockfile, Cargo/Rust for Integrated Preview, and bubblewrap (`bwrap`). Install
prerequisites using the host's existing development setup. No new cloud service
or GitHub Actions workflow is required.

At the inspected Console SHA, oxlint requires Node 20.19+ or 22.12+. Node 20.17
can build the UI but cannot run that lint tool. Verification supplies explicit
`src`, `tests` and `vite.config.ts` roots with `--no-ignore` so the containing
GUI worktree's `.preview/` ignore rule cannot silently skip linting.

```sh
# Fast static UI with a read-only mock API; never starts an Agent.
./preview.sh start --worktree /absolute/corriedale/task-worktree --mode ui

# Real binary from that source; a separate private Agent namespace.
./preview.sh start --worktree /absolute/corriedale/task-worktree --mode integrated

./preview.sh status PREVIEW_ID
./preview.sh open PREVIEW_ID
./preview.sh smoke PREVIEW_ID
./preview.sh logs PREVIEW_ID --component frontend --lines 100
./preview.sh logs PREVIEW_ID --component backend --lines 100
./preview.sh logs PREVIEW_ID --component build --lines 100
./preview.sh stop PREVIEW_ID
./preview.sh cleanup PREVIEW_ID
```

`start` prints the ID immediately to stderr (so failed builds can be inspected),
then JSON with branch, SHA, dirty state, source digest, artifact digest, process
identity, build/health/API results and URL. Sign in using the generated private
`password_file`. Credentials never appear in URLs or logs. The password gate
protects both mock and real preview API access; it does not replace Agent auth.

State defaults to this tool worktree's `.preview/`, ignored by Git. Choose a
different **empty** state directory using `./preview.sh --root /short/path …`;
all commands must use the same root. The root is private (0700) and ownership
marked. Long paths can exceed Linux's Unix-socket limit; use a shorter root.

Default start detaches. `--foreground` keeps a shell/tool session alive and stops
on Ctrl-C. Status and stop use the private authenticated control socket when
shell tools have different PID namespaces. In the same namespace, stop verifies
boot/start-time identity, the exact preview path and process group before signals.
No broad name-based process kill is used. `stop` retains artifacts and logs;
`cleanup` deletes only that ID's owned directory after stopping it.

## Isolation and provenance

Each start allocates a new ID, reserved localhost port, source snapshot,
artifact directory, logs, home/cache directories and runtime directory. It
never uses a prior build as a new success. A snapshot that changes while
copying fails. Symlinks in source are refused. `node_modules`, `dist`, `target`,
Git metadata and ignored files are not copied; never put needed preview inputs
in ignored `.env` files. `VITE_API_BASE_URL` is forced to same-origin.

Builds run with a read-only host filesystem, only that preview directory
writable, private temporary files/PID namespace and no inherited cluster/API
credentials or target environment variables. **Build network access is allowed**
for locked dependency acquisition; this is for authorized local development
source, not an untrusted-code CI security boundary. Keep using Jenkins's
stronger untrusted-build policy for external code.

Integrated runtime has its own network, PID and IPC namespaces and a minimal
filesystem. It has no host NIC, routes, production state, containerd socket,
SSH/Codex credentials or cluster join addresses. A Unix socket bridges requests
to its private loopback Agent. It generates a new operator, node identity and
attestation and explicitly requires signed Agent authentication. Runtime
isolation failure fails the start; there is no host-network fallback.

`artifact-hashes.json` identifies the built Console files and Agent binary;
`manifest.json` binds them to the source digest, commit and dirty state. Serve
these copied artifacts, not a worktree that continues changing. Start a new
preview after further edits. Preview state is local evidence, not a signed
release attestation. Avoid editing its artifacts while it is running.

Real-cluster targets and external API targets are intentionally unsupported.
A future connector must require an explicit target and separate confirmation,
begin read-only, retain existing auth/TLS, and have independent cluster evidence.

## One-command verification

```sh
./preview.sh verify --worktree /absolute/corriedale/task-worktree --mode ui
./preview.sh verify --worktree /absolute/corriedale/task-worktree --mode integrated
```

`verify` runs Console typecheck/lint/tests, then build/start/health, authenticated
document smoke, unauthenticated-write denial and Console API accessibility,
and opens the browser. Integrated mode additionally runs `cargo fmt --check`,
clippy, library tests and the existing security inventory `--validate`, builds
the Agent, and checks its health and Node Identity API. Logs identify each check.
`smoke.json` persists automatic results; neither browser launching nor automated
Chromium testing records human visual approval. A blocked Console API makes
`verify`/`smoke` exit nonzero, even if document rendering and backend health pass.

The existing Production Release Gate stays `NOT_RUN`. Preview success does not
satisfy it. UI mock success also does not establish backend feature completion.
OCI jobs/WASM workloads cannot run in the default runtime (no containerd and no
optional WASM build); operations requiring them must remain `NOT_RUN`/blocked.

## Remote HTTPS via Tailscale (operator action)

See [Tailscale Serve documentation](https://tailscale.com/docs/features/tailscale-serve).
Serve is restricted to the tailnet and supports HTTPS. Tailnet access and
Console/Agent authorization are separate requirements. Use ACLs/grants to allow
only approved users/devices on the chosen HTTPS port; obtain operator approval
before changing existing policy, certificates or Serve configuration.

1. Inspect `tailscale status` and `tailscale serve status --json` read-only.
   Select an unused port (for example 8443), enable HTTPS/MagicDNS through the
   tailnet's approved process if needed, and record the existing configuration.
2. Start with the **actual** hostname and port:
   `./preview.sh start --worktree PATH --mode ui --remote-host DEVICE.TAILNET.ts.net:8443`.
   This option only adds an accepted Host/Origin. It publishes nothing.
3. Record the intended port, target URL and baseline in that ID's
   `remote-route.json`. Its presence prevents cleanup from erasing route evidence.
4. After explicit operator authorization, in a dedicated foreground terminal:
   `tailscale serve --https=8443 http://127.0.0.1:PREVIEW_PORT`.
   Verify Host forwarding and `https://DEVICE.TAILNET.ts.net:8443` through the
   actual proxy. Do not relax Host, Origin, cookie, Agent auth or TLS checks if
   the proxy configuration differs. Never run Funnel, `serve reset` or firewall
   changes as part of preview automation.
5. From the approved smartphone with Tailscale connected, sign in with the
   preview password, then use the existing Console authentication if supported
   by that source. Check Dashboard/Nodes and the intended operation. Also test
   unauthorized write denial and a device outside the granted access set.
   Record physical-device/browser, network, screens, outcomes and errors.
6. Stop the foreground Serve process. Inspect `serve status --json` and compare
   the baseline. If a route remains, remove **only this owned port/target** with
   `tailscale serve --https=8443 off`, after confirming it was not reassigned.
   Verify it is gone, then remove this preview's `remote-route.json` and use
   `preview stop`/`cleanup`. Do not remove other routes.

The CLI never changes Tailscale settings and cannot automatically remove routes
created by an operator. Remote route cleanup is an explicit runbook step.
Mobile/external-network verification remains `NOT_RUN` until a real device
performs it; a simulated viewport is insufficient.

## Completion report

Include Preview ID, source commit + dirty/source digest, running/stopped state,
Console URL, screens/operations tested, actual tests, blocked/NOT_RUN items,
human visual-review result, and whether the preview remains running. Include
the start failure and log path when environment constraints prevent launching.
See [measured validation](console-preview-validation.md) for this implementation.
