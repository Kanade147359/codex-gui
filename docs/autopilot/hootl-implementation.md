# Three-layer HOOTL implementation record

## Baseline and smallest implementation plan

The existing Mission Controller is `app/autopilot/controller.py`, driven by
TaskManager, with the existing SQLite ledger, worktrees and Codex thread reuse.
Reuse its fixed-candidate verification, independent reviewers, bounded repair,
dependency claims, quota ledger, pause/stop, reconciliation and workflow UI.
Do not create a second scheduler or deploy/merge to main.

1. CLI adapters: inspect installed CLI flags and official authentication docs;
   remove application-side credential decoding/copying, use CLI auth metadata,
   retain fail-closed billing/environment and isolation gates.
2. Workflow: add an opt-in local Ollama DSH dispatcher for approved DAG nodes,
   durable decisions and strict output validation. Add Claude-only auditing policy
   with an atomic acceptance guard. Reuse the existing Codex repair loop.
3. Operations: persist dispatcher claims/results, recover interrupted planning
   without duplicate implementation, expose metrics through existing workflow UI,
   and use bounded exponential reviewer backoff.
4. Verification: deterministic isolated tests, installed CLI metadata/offline
   smoke, then subscription model smoke only with billing confirmation. Record
   unavailable checks and leave the 24-hour run explicitly incomplete.

## Known scope gaps at baseline

DSH/Ollama is absent. Planning/replanning uses Codex. Every candidate requires
independent review; Claude quota switches to independent Codex. There is no
automatic Draft PR publication or aggregate mission metrics. Managed validator
concurrency is bounded, but implementation CPU/GPU/disk are not OS constrained.
These are separate requirements, not evidence of a complete 24-hour system.

## Sources inspected

- [OpenAI authentication](https://learn.chatgpt.com/docs/auth?surface=cli): official CLI login status and authentication routes.
- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference) and
  [authentication](https://code.claude.com/docs/en/authentication): subscription
  login and `auth status`; API/Console routes are not subscription proof.
- [Ollama generate API](https://docs.ollama.com/api/generate) and
  [structured outputs](https://docs.ollama.com/capabilities/structured-outputs):
  local generation with a JSON schema, separately validated by the Controller.

Installed probes: Codex 0.159.2, Claude Code 2.1.185; local Ollama exposes installed
models. CLI versions and authentication metadata do not prove model execution,
remaining subscription allowance, or disabled extra usage.

## Completion status

Implementation and test results are recorded below as work progresses.
The 24-hour real mission is **NOT RUN**. Do not mark the overall request complete
without it and authenticated end-to-end implementation/audit evidence.

## Implemented phases

### Phase 1 — CLI integration

Claude 2.1.185 `auth status --json` reported `claude.ai`, `firstParty`, `pro`;
Codex 0.159.2 `login status` reported ChatGPT. The adapter now asks the official
CLI for metadata rather than reading credential JSON. The CLI's own file is
mounted read-only without application-side token extraction/copying. Codex
review/auth processes also pin `forced_login_method="chatgpt"`. Existing
implementation preflight, subscription checks, structured terminal parsers and
tool-disabled independent review sessions remain in use. API keys, alternate
routes and injected OAuth/access-token environment variables are rejected.
Unknown metadata or missing credentials fail closed. Credential refresh through
a writable store is intentionally not performed by the Controller; normal CLI
login may be needed before resuming. This remains a long-duration limitation.

### Phase 2 — bounded three-layer workflow

`policy.dsh` opts into local Ollama. `/api/show` checks for remote model routing;
there is no model pull, cloud route, API compatibility fallback, proxy inheritance
or redirect following. Generation uses JSON schema, fixed context/token bounds,
CPU-only options, a configurable thread limit and timeout. Controller validation
rejects duplicate JSON keys, unknown fields, truncation and ineligible selections.

`POST /api/autopilot/dsh/plan` decomposes an owner goal into at most eight tasks.
The owner supplies allowed paths and named validation profiles. DSH can choose
profiles but cannot invent verification commands, expand paths or grant authority.
The existing manifest parser rejects invalid DAGs and overlapping ownership.
The original goal becomes a fixed `MISSION.md` input. This endpoint returns a
dry-run proposal; existing registration/opt-in/start controls execute it.

During a running mission the dispatcher chooses from dependency-ready tasks.
The existing TaskManager creates isolated worktrees and runs Codex. Managed
verification gates review. Claude findings use the existing bounded same-thread
Codex repair loop. DSH cannot declare success or waive a check.

`review_provider_policy: claude_only` is required for DSH missions and enforced
again inside the atomic acceptance transaction. Claude quota holds the same
candidate in `waiting_provider`, then performs a serialized bounded recheck.
It never substitutes a Codex review for the required Claude audit. Existing
non-DSH workflows keep `claude_then_codex`. DSH cannot publish `main`.

### Phase 3 — operations and visibility

DSH requests are claimed in the existing workflow-event ledger before inference;
one running dispatcher claim is admitted across workflows. Controller process
identity prevents another live Controller from stealing a request. Completed
decisions survive restart. Interrupted planning waits for explicit Resume and
retains the retry budget. No implementation is blindly duplicated. Stop cancels
planning; late results cannot bypass stopped/paused workflow launch gates.

Transport/server failures use capped exponential backoff. Authentication, missing
models, invalid output and invalid configuration do not automatically retry.
Review transient retries also use exponential backoff. Retry diagnostics are
preserved in the event payload. Existing acceptance, worktree/thread retention,
dependency claims, verification isolation and emergency stop remain authoritative.

The workflow UI/API exposes Codex execution count, accepted-task rate (explicitly
not exit-code success), Claude reviews/findings, DSH decisions/attempts/success
rate, managed validation/check pass rates, verified deliverables, recorded owner
decisions and completed-attempt duration by task. Official Ollama token counts
and durations are retained. Missing recovery-rate and quota-duration telemetry
is `null` with explanatory limits, not fabricated zeroes.

### Phase 4 — evidence and remaining qualification

| Check | Result |
| --- | --- |
| Existing focused Autopilot tests before integration | 81 passed |
| Autopilot + HOOTL regression run after core integration | 297 passed, 14 opt-in skips |
| Final focused HOOTL tests after splitting auth coverage | 35 passed, 1 real-model opt-in skip |
| Installed CLI auth metadata + local scripted CLI responses | 6 passed |
| Full repository suite | 1,022 passed, 36 skipped, 3 failures |
| Same three failures on pristine baseline `c70cb894` in `/tmp` | Same 3 failures reproduced |
| Real local Ollama selection | PASS, ministral-3:3b, 127 prompt / 50 generated tokens |
| Real local Ollama goal decomposition | PASS; see [saved evidence](hootl-local-planner-smoke.json) |
| JS syntax checks | Both changed scripts passed `node --check` |
| Real Codex implementation + Claude model audit | NOT RUN; billing confirmation pending |
| 24-hour live mission / actual exhausted subscription / OS restart | NOT RUN |

The full-suite failures are `test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded`,
`test_real_codex_tool_output_limit_reaches_the_model` (both `test_ctx_e2e.py`) and
`test_tool_output_token_limit_only_lowers_the_cap` (`test_ctx_integration.py`).
They reproduce on unchanged HEAD with the same installed CLI; no assertions were
weakened. Test repositories, databases and worktrees were temporary. No production
GUI process, production database, live/main checkout or other task worktree was
modified. Real local Ollama requests used the existing service without reconfiguring it.

The minimum completion contract is **not met**. Outstanding implementation:

- Selective audit eligibility for minor changes. Currently every candidate still
  needs independent review; required Claude audits cannot be skipped.
- Automatic Draft PR saving for mission outputs. Accepted commits/worktrees and
  artifacts persist locally, but the Controller does not publish PRs yet.
- Continuous DSH improvement hypotheses and semantic recovery decisions beyond
  bounded goal decomposition/dispatch; existing Codex replanning still handles them.
- Implementation-level CPU/GPU/disk quotas and task wall-clock enforcement.
  Existing managed validator/reviewer timeouts and slot limits, plus DSH options,
  are not OS/cgroup limits on all implementation processes.
- Complete recovery success, human-intervention and quota-wait interval metrics.
- Real-model end-to-end and 24-hour qualification, including credential refresh.

## Running the isolated environment

Install the existing development requirements in a worktree-local virtualenv.
Start an isolated UI (never point this command at production data):

```sh
CODEX_GUI_HOME="$(mktemp -d /tmp/hootl-dev-XXXXXX)" \
  CODEX_GUI_MAX_CONCURRENT=1 .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8766
```

Open `/autopilot`, choose the repository/base, fill in the local DSH goal, installed
model, allowed paths and verification profiles, then generate a plan. Inspect its
dry-run before Start. Ordinary manifests can opt in with:

```json
{
  "dsh": {"model": "ministral-3:3b", "endpoint": "http://127.0.0.1:11434", "timeout": 90, "num_thread": 2},
  "review_provider_policy": "claude_only",
  "extra_usage_disabled_confirmed": false,
  "mentor_enabled": false
}
```

This is the `policy` object, not a whole manifest. Set the billing confirmation
only after confirming both accounts have extra usage/credits disabled. The CLI
metadata smoke cannot establish that billing setting.

```sh
.venv/bin/python -m pytest -q tests/test_hootl.py tests/test_hootl_auth.py tests/test_autopilot*.py
CODEX_GUI_AUTOPILOT_AUTH_SMOKE=1 CODEX_GUI_AUTOPILOT_CLI_SMOKE=1 \
  .venv/bin/python -m pytest -q tests/test_autopilot_auth_smoke.py tests/test_autopilot_cli_smoke.py
```

Only after the explicit account confirmation, the prepared real integration test
can be run using `CODEX_GUI_HOOTL_REAL=1`,
`CODEX_GUI_EXTRA_USAGE_DISABLED_CONFIRMED=1`, and an installed model name in
`CODEX_GUI_OLLAMA_MODEL`, with `.venv/bin/python -m pytest -q -s tests/test_hootl_real.py`.
It saves evidence in pytest's temporary directory and fails rather than accepts
on authentication, quota, planner, verification or review failure. It does not
qualify a 24-hour run.
