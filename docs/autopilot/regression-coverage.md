# Regression and isolated acceptance evidence

All cases use temporary SQLite databases, repositories/worktrees and fixture CLIs.
Real installed-CLI checks are explicit opt-in and use a localhost scripted endpoint
or auth metadata only. No real model/quota exhaustion or production state is used.

| ID | Coverage | Expected result checked |
| --- | --- | --- |
| T01 | review-before-success regression | completed needs_review starts review; ordinary dependent remains waiting |
| T02 | acceptance/successor regression | terminal approval plus managed checks accepts once and launches successor once |
| T03 | manifest and omitted-validator regressions | empty/unexecuted mandatory commands never produce acceptance |
| T04 | quota subprocess fixture | same fixed candidate switches from Claude quota to independent Codex, reason retained |
| T05 | strict parser fixtures | README/tool/assistant quota strings are not provider signals |
| T06 | typed/error/parser regressions | auth, config, overload, timeout, limits and broken/duplicate JSON stay distinct and bounded |
| T07 | both-provider quota fixture | waiting_provider holds no implementation slot; no paid/credit path |
| T08 | repair and integration-repair fixtures | changes request repairs original task/thread; first integration fix creates a thread only when none exists |
| T09 | partial-finding fixture | quota-interrupted findings survive fallback; unresolved findings prevent acceptance |
| T10 | head/contract/input/turn regressions | old scope or owner requirements cannot approve current work; leaf turn gets a new candidate |
| T11 | concurrent claim and schedule regressions | no duplicate review/acceptance/turn; scheduled source turns wait for acceptance and remain serial |
| T12 | restart/uncertain/merge reconciliation | PID identity recorded; unknown CLI outcome never blindly replayed; known merge checks actual refs/index/all pinned SHAs without another merge |
| T13 | Stop/quota-reset fixture | old completion/reset cannot launch successor/fix/review after Stop |
| T14 | repair-budget fixtures | exactly three automatic fixes stop at budget; two unchanged rounds stop at no-progress; independent branch succeeds |
| T15 | missing-artifact/command coverage | mandatory artifacts/commands cannot be replaced by model APPROVE; test changes remain in full reviewed diff |
| T16 | namespace/filter/catalog negatives | GUI DB/auth/home inaccessible; read-only source; hooks/filter/fsmonitor/model tool side effects denied |
| T17 | A/B/C → I1 → D E2E | accepted input SHAs merged; reopened GUI/SQLite resumes without Send/Approve/merge; D reads integrated code |
| T18 | stacked/contained/missing SHA regressions | contained commits skipped; missing input is not treated as a resolvable conflict |
| T19 | real Git conflict fixture | dedicated Codex integration task resolves index; combined candidate reruns validation and independent review |
| T20 | moved-main fixture | compare-and-swap target/live checkout guard retains candidate and stops safely |
| T21 | real bubblewrap fixture | source tree unchanged; explicit scratch/build output writable; no GUI filesystem access |
| T22 | migration and existing suites | additive migration, legacy completion, normal all_success, retry and thread behavior preserved |
| T23 | API/manifest/idempotency negatives | duplicate import/key, cycle, bad paths/YAML/zip rejected without partial tasks |
| T24 | cross-workflow caps fixture | three implementation slots, one formal validation and review; no deadlock; priority with aging |
| T25 | owner/manual-gate fixture | AI cannot clear design/manual approval; independent slice completes |
| T26 | old-attempt/cancelled-event regression | candidate/run/review generations rejected; cancelled/uncertain records cannot be overwritten by a late success |

The vertical fixture uses real Git commits/merges and the actual controller runner,
fake implementation subprocesses, and fake review subprocesses. A needs a correction
in the same source thread; B records Claude quota and independent Codex fallback;
C completes normally. SQLite is reopened and a fresh GUI TaskManager/controller
continues I1/D. No Pause/Resume, Send, Approve or manual merge is needed after Start.
Source repository/main remain at the initial fixture SHA. The summarized fixed-SHA
ledger is saved in `e2e-evidence.json` with provider attempts and accepted input refs.

Full-suite existing failures are compared against unchanged baseline
`405d722daab33e8a5ff4413373a9f384f80b47b9` in a separate `/tmp` extraction.
The three Codex tool-output integration failures reproduce there; no assertion was
weakened. Exact final counts/commands/source SHA are in `verification.json`.
