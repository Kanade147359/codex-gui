# Provider compatibility — 2026-10-14

Historical bootstrap record; current [document workflow compatibility](document-workflow-verification.md) includes bounded snapshot reads and prepared toolchain support.
The [2026-10-15 goal/replanning record](verification/goal-aware-replanning/verification.json)
adds installed-CLI review-v2 and replanner-schema smoke, adapter version separation,
fresh analyst sessions, bounded snapshot reads, cancellation and shared quota checks.
These use local scripted responses and isolated auth metadata, not real models or
measured quota exhaustion. Operational model acceptance remains NOT_RUN.

Tested code: `6040c605a4e2578be072b6d991590474b8cb7d5a`.

| Component | Observed result | Boundary |
| --- | --- | --- |
| Claude Code | Installed `2.1.185`; print/stream-json/json-schema/empty-tools/safe-mode/settings-source/strict-MCP flags inspected | No real review/model turn was started |
| Claude auth | Private staged OAuth credentials, `--safe-mode`, neutral cwd and auth/status: logged in, `claude.ai`, first-party route | Metadata only; access entitlement, model output and extra-usage state are not verified |
| Claude quota parser | Version-coupled synthetic structured `rate_limit_event` fixtures, including reset/bucket/overage discriminator | Not a measured exhausted subscription; unknown versions/signals do not imply quota |
| Claude result parser | Terminal result envelope, normal process exit, nonempty session identity, exact schema/scope, duplicate-key rejection | Verified with subprocess fixtures, not a real paid/subscription response |
| Codex | Installed `0.159.2`; generated local RPC schema inspected | Local installed version, not an assumption from web documentation |
| Codex auth/session | Isolated app-server with only staged ChatGPT auth; account/read confirms ChatGPT; two fresh thread/start IDs differ | No model, limit, reset-credit or account-setting operations in auth smoke |
| Codex parser/cancel | Installed app-server against localhost scripted Responses fixture: fresh sessions, outputSchema, terminal parser and active turn interruption | No real model/quota; test-only endpoint override never appears in personal production adapter |
| Codex tool surface | Installed catalog with shell/plugins/subagents/goals disabled has no exec_command/MCP; runtime tool events discard the review | Source read-only, allowlisted runtime, no GUI DB/home/SSH/socket binds; scratch only |
| Validation | Real bubblewrap, read-only source, private scratch, registered empty build outputs, no network, fixed timeout and bounded redacted logs | Host `/usr`/system runtime must contain needed dependencies; private user runtimes and online setup are not implicitly exposed |

Both review adapters stage credentials privately and mount them read-only. A required
refresh can stop for owner login; original credential/account state is not modified.
Source hooks, Git filters/fsmonitor, project settings, MCP and plugins are not executed
by the reviewer/snapshot path. Unchanged AGENTS.md is not transcribed into prompts;
a changed AGENTS.md is reviewed as candidate data. Reviewer prompts contain the
fixed full base/head diff and a bounded source bundle, not implementation conversation.

Personal billing preflight refuses API keys, alternate routes and missing owner
confirmation that extra usage/credits are disabled. This owner confirmation has not
been supplied for bootstrap real-model smoke. The GUI cannot automatically verify
that account setting, so enabled extra usage remains an explicitly reported risk.
No billing route, credit consumption/purchase or account setting was changed.

Operational real-model acceptance is **NOT_RUN**. Fixture success establishes
controller behavior and compatibility smoke, not model review quality, real quota
event shape, available model entitlement, or a guarantee of subscription billing.

Official references used alongside installed CLI inspection:

- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude headless mode](https://code.claude.com/docs/en/headless)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
