# Codex GUI

**English** | [日本語](README.ja.md)

A local web GUI for running several Codex CLI instances at once and managing them from a single screen. Codex is driven through **`codex app-server`** (threads / turns).

- 1 task = 1 Git branch = 1 Git worktree = 1 Codex thread. A follow-up instruction is simply the next turn of the same thread (`thread/resume` + `turn/start`), so Codex keeps the conversation history and the prompt cache (cached input) stays effective. The GUI never re-pastes history
- Defaults chosen to make the most of a flat-rate plan (GPT-6.1 Sol / Standard / Low / low verbosity / web search left at Codex's own default, cached), plus cache hit, context and usage-limit displays ([Usage Optimization](#usage-optimization))
- Edit `AGENTS.md` for the repository and for each task worktree in the browser ([AGENTS.md Editor](#agentsmd-editor))
- Worktrees are managed by the GUI (Codex's `--worktree` is not used)
- Run multiple Codex processes in parallel, watch logs in real time, and inspect Git status / diff / log
- Stop, Commit / Push, delete worktrees / branches, and keep history in SQLite

> **Note:** The GUI itself has no login (authentication). Read [Security](#security) before exposing it to anyone else.

## Table of Contents

**Getting Started**

1. [Requirements](#requirements)
2. [Getting Started](#getting-started)
3. [Usage](#usage)
4. [Security](#security)

**Features**

5. [Signing in to Codex](#signing-in-to-codex)
6. [Follow-up Instructions](#follow-up-instructions)
    - [Image Attachments](#image-attachments)
7. [Usage Optimization](#usage-optimization)
8. [Context Efficiency](#context-efficiency)
9. [AGENTS.md Editor](#agentsmd-editor)
10. [Task Dependencies](#task-dependencies)
    - [Scheduled Instructions](#scheduled-instructions)
11. [Automatic Recovery](#automatic-recovery)
12. [Git Worktree Handling](#git-worktree-handling)
13. [Restarting the GUI](#restarting-the-gui)
14. [Auto Approval and Network Access](#auto-approval-and-network-access)

**Reference**

15. [Environment Variables](#environment-variables)
16. [SSH Authentication](#ssh-authentication)
17. [Data Location](#data-location)
18. [Testing](#testing)
19. [Limitations](#limitations)
20. [Project Layout](#project-layout)

## Requirements

- Linux / WSL2, Python 3.10+, git
- The `codex` command must be installed (`codex app-server --help` must work). You can sign in to your ChatGPT account from the GUI dashboard ([Signing in to Codex](#signing-in-to-codex)); running `codex login` beforehand also works
  - Tested version: codex-cli 0.159.2 (research notes: [docs/codex-capabilities.md](docs/codex-capabilities.md))
  - For older CLIs without app-server, use `CODEX_GUI_BACKEND=exec` (the legacy `codex exec` mode; usage-limit display, follow-ups to a running turn and compact are unavailable)

## Getting Started

```bash
git clone https://github.com/Kanade147359/codex-gui.git
cd codex-gui
./run.sh
```

The first run creates `.venv` and installs the dependencies. Once it is up, open <http://127.0.0.1:8765> in a browser (on WSL2, a browser on the Windows side works).

Settings can be changed with environment variables ([Environment Variables](#environment-variables)). If you push to SSH remotes, also see [SSH Authentication](#ssh-authentication).

## Usage

1. If needed, sign in to Codex from **Codex sign-in** at the top of the dashboard ([Signing in to Codex](#signing-in-to-codex))
2. Click `+ New Task`, fill in the form, and Run (everything can be chosen in the GUI)
   - **Repository:** Pick a folder on the server with `Browse…` (git repositories carry a `git` badge). Recently used repositories are available as one-click chips. After you choose one, `Git: clean` / `AGENTS.md: Found | Not found` is shown
   - **Base ref:** Choose from the repository's branches / existing worktrees / remote branches / tags (`Custom…` accepts any ref or commit).
     If you pick a worktree, the new task branches from its **committed state** (uncommitted changes are not included)
   - **Model / Reasoning / Speed:** Defaults are GPT-6.1 Sol (when this codex recognises it) / Low / Standard. The choices follow `codex debug models` (efforts and tiers supported per model)
   - **Auto approve / Web search / Network access / Adaptive reasoning / Context guard**, and **Advanced settings** (output verbosity, sandbox, extra writable directories, feature flags)
   - **Run & add another:** Keep the form open and add the next task. You can **run multiple tasks in parallel on the same repository** (each task gets its own branch and worktree)
   - **After other tasks complete:** Start automatically once other tasks have finished ([Task Dependencies](#task-dependencies))
3. A worktree and branch are created, and Codex starts inside it (the dashboard refreshes every 2 seconds)
4. The dashboard can be filtered by repository. Click a row to open the detail page, where you can inspect the Codex log (click an event type to see the raw event JSON) and Git Status / Diff / Log
5. Send follow-ups from **Additional instruction** on the detail page ([Follow-up Instructions](#follow-up-instructions))
6. Stop / Commit / Push / Delete Worktree / Delete Branch are all on the detail page ([Git Worktree Handling](#git-worktree-handling))

The diff shown is the **difference from the base commit** (including commits Codex made), plus untracked files.

## Security

The GUI itself has no login (authentication). Anyone who can open it can drive Codex on the server — that means running commands, editing files and pushing to Git.
By default it listens on `127.0.0.1` only. If you expose it to other machines, put it behind an authenticated reverse proxy or a VPN (Tailscale / WireGuard, etc.).
([Signing in to Codex](#signing-in-to-codex) is a sign-in to Codex (your ChatGPT account), not a login to the GUI.)

## Signing in to Codex

When Codex is not signed in to a ChatGPT account (or is signed in with an API key), a **Codex sign-in** panel appears at the top of the dashboard. It does what `codex login` does, from the GUI.

| Button | What it does |
| --- | --- |
| **Sign in with ChatGPT (open browser)** | Opens the sign-in page in a new browser tab (press **Open sign-in page** if it could not be opened). When sign-in finishes, this screen refreshes automatically and the panel disappears. The post-sign-in redirect goes to **localhost on the machine where Codex runs**, so do this from a browser on that same machine |
| **Use a device code** | Enter the displayed code on the page (**Open sign-in page**). Works from a browser on a different machine (use this when you open the GUI remotely) |
| **Cancel** | Cancels a sign-in in progress |

- When signed in, the dashboard shows `Codex: signed in as <email> (<plan>)`.
- Passwords and tokens never pass through the GUI. The browser talks to OpenAI directly, and the result is stored in Codex's own location (`~/.codex`). The GUI stores nothing.
- On failure the reason is shown. Signing in with an API key is not possible (with the default `CODEX_GUI_SUBSCRIPTION_ONLY=1`, tasks do not start under API-key authentication either). There is no sign-out action (use `codex logout` if you need it).
- Under the hood this uses the app-server methods `account/read` / `account/login/start` (`chatgpt` / `chatgptDeviceCode`) / `account/login/completed` / `account/login/cancel`. The API endpoints are `GET /api/codex/account`, `POST /api/codex/login` (`{"method": "browser" | "device"}`) and `POST /api/codex/login/cancel`.

## Follow-up Instructions

A task keeps using a single Codex thread.

| Situation | Action | What happens |
| --- | --- | --- |
| Creating a task | Run | `thread/start` (settings are fixed) → `turn/start`. The thread id is saved to `tasks.codex_thread_id` |
| Stopped / after completion | **Send** | `thread/resume` (same settings as at creation) → `turn/start`. The next turn of the same thread |
| **While running** | **Send to running turn** | `turn/steer` (adds to the running turn). app-server backend only |
| After completion | **Compact Thread** | `thread/compact/start` (with confirmation; task / worktree / branch / thread are kept) |
| Any time (once stopped) | **Start New Session** | A new thread in the same worktree. The old thread's conversation and cache are not carried over. With confirmation |

- The text you enter is sent **as is**. The GUI never adds timestamps, IDs or earlier exchanges (history lives in the Codex thread; the GUI keeps the instruction text in its log for display only and does not resend it).
- Model / reasoning effort / Speed / sandbox / approval / web search are **fixed when the task is created and the same values are sent on every turn** (there is no UI for changing them midway, because changing them alters the prompt prefix and weakens the cache).
  The only exception is the reasoning effort when you press **Retry with Medium / High** (only when pressed; it is recorded in the log).
- `codex queue` is not used: in codex-cli 0.159.2, a message sent with `codex queue` to a running `codex exec` ends up in the thread history, but
  exec aborts that turn with `turn_aborted` and exits, so the message is **lost without being answered**. The app-server's `turn/steer` is used instead.
  With the `exec` backend, follow-ups to a running turn are not possible (Send is disabled; the API returns 409).
- If Codex does not report a thread id, this is logged and Send is unavailable for that task (Start New Session still works). If a different thread id comes back on resume, a `WARNING` is logged.
- The thread stays on the Codex side even if you restart the GUI, so it can be resumed from `codex_thread_id` in the DB (verified on a real Codex).

## Image Attachments

New Task and Additional instruction accept screenshots/reference images together with text (or images alone).
Use **Attach images**, paste a clipboard image with **Ctrl+V** (Cmd+V also works), or drop files onto the input/attachment area.
Each image shows its filename and thumbnail; **Remove** removes it from the current draft, and clicking the thumbnail opens an enlarged view.
Normal text paste and Japanese IME input keep their usual behaviour. Sent messages and their images appear under **Message history & images**;
reserved messages also show images in **Scheduled instructions**.

PNG, JPEG and WebP are supported: **8 images per message**, **10 MiB per image**, **40 MiB total**, **8192 pixels per side**,
and **32 million pixels** per image. Animated, corrupt and oversized files are rejected by the backend based on the actual bytes,
not the filename/MIME header. These limits live in `app/attachments.py`.

Uploads are copied into `$CODEX_GUI_HOME/attachments` under generated IDs; ordered references are stored in SQLite per message and scheduled instruction.
The original file can be removed. Restart, delayed execution and recovery keep the saved attachments. Back up the database and attachment directory together.
Removing a preview, cancelling a reservation or deleting a worktree does **not** delete stored images that a history or another task may reference.
Automatic cleanup of unused uploads is not implemented, so removed/abandoned uploads also remain on disk.
Uploaded drafts and their text are restored on page reload using browser local storage when available; a file whose upload has not succeeded must be selected again after reload.

The exec backend passes separate `--image` argv entries to `codex exec` or `codex exec resume <thread_id>`; the shared app-server uses `localImage` inputs.
A follow-up includes only its own images. A retry resends its images only if Codex never confirmed that the instruction started;
otherwise recovery continues the existing thread without reattaching past images. Running-turn input follows the existing rules
(app-server: steer; exec: wait, or reserve a Scheduled Instruction).
The configured CLI is checked for image support. Unreadable images/unsupported CLI versions produce an error and preserve input rather than sending only the text.
HTTP submission/upload failures leave the draft available for retry.

On WSL, images selected or pasted in a **Windows browser** are uploaded as bytes and saved to paths readable by the **WSL Codex CLI**;
Windows client paths are never passed to it. Use a WSL-native `CODEX_BIN`. Launching a Windows `codex.exe` from WSL with image attachments is rejected with an explanation.

Optional checks, always using temporary GUI/repository data:

```sh
CODEX_GUI_REAL_IMAGES=1 .venv/bin/python -m pytest tests/test_real_images.py -s
# Chromium UI checks (Playwright is an optional test dependency):
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
CODEX_GUI_BROWSER=1 .venv/bin/python -m pytest tests/test_ui_attachments_browser.py
```

The real-image check uses subscription auth in an isolated `CODEX_HOME` and verifies both new and continued image recognition on exec and app-server.

## Usage Optimization

The goal is not frugality but to **reduce wasted context sending, session re-creation, unnecessary high reasoning, and uncached input**.
There is no usage-limit evasion, automatic fallback to an API key, multi-account support, automatic pacing to "burn through the allowance", or parallelism computed from the usage limit.

### Defaults

| Setting | Default | Notes |
| --- | --- | --- |
| Model | GPT-6.1 Sol (when this codex lists it), otherwise Codex default | `Use Codex default` / `Other…` are also selectable |
| Reasoning | **Low** | `Auto` = the Codex / model default. Only values supported by the model can be chosen (Low–Ultra) |
| Speed | **Standard** | Choosing Fast shows a small note: *Fast mode consumes included usage more quickly.* |
| Output verbosity | **Low** (Advanced) | `model_verbosity` |
| Auto approve | **ON** | Equivalent to `--approve-for-me` ([Auto Approval and Network Access](#auto-approval-and-network-access)) |
| Sandbox | **workspace-write** (Advanced) | `read-only` is also selectable. `danger-full-access` is not |
| Web search | **Cached** | Matches Codex's own default (web search is on by default in 0.159.2). **Cached** = results from OpenAI's search index, **Live** = the real web, **Off** = disabled (this does not stop Codex from reading local files). The value is fixed when the task is created. Earlier tasks keep Live if they had it ON and Off if they had it OFF |
| Network access | **ON** | Network inside the workspace-write sandbox (`git fetch` / `git push`, `gh`, installing packages, etc.). With OFF, all network traffic inside the sandbox fails (local `git status` / `diff` / `log` still work). Separate from Web search (the model's search tool). Irrelevant with `read-only`. Tasks created before this option stay OFF |
| Adaptive reasoning | ON | See below |
| Context guard | ON | See below |
| Use subscription authentication only | ON (shown, not changeable) | See below |

- **Ultra** ("Maximum reasoning with automatic task delegation") is used **only when you choose it explicitly**. Choosing it shows *Ultra may use substantially more compute.* It is never selected or escalated to automatically.
- **Adaptive reasoning** only highlights and recommends **Retry with Medium / High** on the detail page when Codex itself reports a turn as `failed` (and it is not a quota stop).
  It **never raises the effort automatically** (nor on the basis of a process exit code alone). Suggestions go up to High; XHigh / Max / Ultra are never suggested automatically.
- Common working guidelines (don't read huge files or logs whole; use `rg`, ranged `sed`, `head` / `tail`, narrow tests, short output) are passed as `developerInstructions` **once, when a new thread starts** (not resent every turn).
  The default text is in [app/instructions.py](app/instructions.py). Placing `$CODEX_GUI_HOME/instructions.md` replaces it, and leaving it empty disables it. The repository's `AGENTS.md` is never modified automatically.
- **Keeping the prompt cache intact**: the fixed instructions the GUI adds are constants and contain no variable values such as timestamps, PIDs, task IDs, UUIDs, worktree creation times or quota (this is tested).
  The settings above are not changed within a task.

### Cache and usage

The task detail shows the latest turn's Input / Cached / Uncached / Cache hit / Output (and Reasoning, if any), a **per-turn table**, and the dashboard's **CACHE** column shows the latest turn's hit rate.

- The app-server's `thread/tokenUsage/updated` (for exec, `turn.completed.usage`) is the **cumulative value for the thread**. The GUI stores the difference from the previous cumulative value as "that turn's usage" in the `turns` table.
  If the cumulative value decreases, it is treated as a per-turn value and the raw value is used (this is logged).
- Cache hit = `cached_input_tokens / input_tokens × 100`. Not shown when `input_tokens == 0`.
- Compaction also consumes tokens, so it is kept in `turns` as a row with `kind = compact` (shown as `(compact)` in the table).
- The prompt cache is best-effort. Sending turns back to back can give a low hit rate (measured: 76% → 76% → 99%).

### Context and Context Guard

The **Context** panel on the task detail (Current / Model window / bar) and the dashboard's **CTX** column. The context size is the `last.totalTokens` reported by Codex, and the window is `modelContextWindow` (from the model's metadata; the GUI hard-codes no values).

- **Context Guard** (ON by default): above 80% of the window (`CODEX_GUI_CONTEXT_WARN_PERCENT`), it shows *This thread has become large. Compaction may reduce repeated context processing.* and a **[Compact]** button (warning only; no automatic compact).
- **Compact Thread**: runs `thread/compact/start` after a confirmation dialog. The context size after compaction is known only on the next turn (unknown until then).

### Codex Usage (usage limits)

The top of the dashboard shows the result of `account/rateLimits/read` (every 15 seconds; cached for 30 seconds on the server).

```text
Codex Usage (prolite)
Weekly   ██░░░░░░░░   11%   Reset: Oct 8 18:20
Running: 4
```

- Windows are classified as **5 hour / Weekly** by `windowDurationMins`. **Depending on the plan, only a single weekly window is returned and there is no 5-hour window** (only weekly in the environment checked; some plans return two). Windows that do not exist are not shown.
- `Available resets: N` (number of available resets) is display only. **The GUI never uses a reset.**
- Usage-limit history is saved to the `rate_limit_history` table (always at the start and end of each task turn; update notifications from Codex at most once every 5 minutes). It is for analysing per-task consumption later and is **not used for automatic control**.
  It can be fetched from `GET /api/limits/history?task_id=...`.
- **Observed quota change** on the task detail: the usage percentage before and after the latest turn (`5 hour 31% → 33%` / `Weekly 12% → 13%`).
  These are coarse integer percentages, so it says *Observed only; not an exact per-task cost.* and **does not attribute the change to a single task when other tasks were running at the same time** (it says so).

### When the usage limit runs out

When Codex says the allowance cannot be used (`ordinaryUsageAllowed: false`, `rateLimitReachedType`, a `usageLimitExceeded` / `rateLimitExceeded` error), the task becomes **`waiting-for-quota`** (*Waiting for Codex quota* on the dashboard).

- **It is not retried automatically.** It does not move to any other billing path either. Once the allowance is back, use **Retry last instruction** on the detail page to resend the last instruction to the same thread.
- If it is known before starting that there is no allowance, the turn is not started.
- If Codex merely fails with an error and goes to `failed`, it is not treated as a quota problem.

### Subscription authentication only

- Before each turn, `account/read` is used to confirm that authentication is ChatGPT (`type: "chatgpt"`). With API-key authentication or when not signed in, the task **does not start and becomes `failed`** (the reason is shown). It never switches to API-key billing.
- `OPENAI_API_KEY` / `CODEX_API_KEY` / `OPENAI_BASE_URL` and similar variables are not passed to the codex child process.
- Disable with `CODEX_GUI_SUBSCRIPTION_ONLY=0` (ON by default; not changeable from the GUI).

## Context Efficiency

Features for cutting wasted tokens / context without lowering quality when many tasks run in parallel. **What actually helps (measured on a real Codex) is written up in
[docs/context-efficiency.md](docs/context-efficiency.md)**. In short:

- **AGENTS.md is never edited automatically.** The Health Check (in the New Task preview and Task Detail) only warns about the chain, size, budget usage, duplicates and "always read X" style instructions;
  fixing them is up to you via the Edit button.
- **Context efficiency** in New Task: Tool output limit / Tool profile (Full · Development · Minimal) / Allow subagents (OFF by default), and under Advanced the Skills catalog budget and
  Working directory. Settings are fixed at task creation and frozen within a thread. A tool profile is shown as "optimized" only when a reduced tool count was actually measured on a real Codex.
- **Context efficiency** in Task Detail: Context and Compactions, Cache age (HOT / WARM / COLD; informational only — no prompt is sent to keep the cache warm),
  per-turn cache read / write / uncached with likely causes of cache misses, large tool outputs, and a long-context warning
  (at `≥272K` it offers **Continue / Compact / Start New Session in Same Worktree**; no automatic compact).
- Sending is **Send Standard** (normal) or **Send Fast** (explicit action). The requested speed is saved for each turn.
- Quota exhaustion, context overflow, authentication errors and repeated failures of the same tool are not retried endlessly; the state is shown and the task stops.
- Thresholds can be changed under **Efficiency settings** on the dashboard.

## AGENTS.md Editor

So that project-wide rules can live in `AGENTS.md` instead of being sent in every prompt, the GUI lets you view and edit it. The GUI never adds the contents of `AGENTS.md` to the prompt (Codex loads it itself, which avoids duplicated prompt / context and a shifting cache prefix).

| Kind | Target | How to open |
| --- | --- | --- |
| **Repository AGENTS.md** | The `AGENTS.md` of the **repository itself (main checkout)** | `[AGENTS.md]` shown when you select a repository on the dashboard (`/agents?repository=…`). Also from the New Task form |
| **Task Worktree AGENTS.md** | The **copy inside that task's worktree** | `[Edit Worktree AGENTS.md]` on the task detail (`/tasks/<id>/agents`) |

These are **two separate files**. The kind is shown on screen, editing a task worktree does not affect main (and vice versa), and passing a task's worktree to the repository editor is rejected.
A task's worktree receives the base ref's `AGENTS.md` at creation. Editing main afterwards does not change the copy in an existing worktree.

- When the file **does not exist**, you see `AGENTS.md does not exist` and `Create AGENTS.md`. Save creates it.
- Textarea based (monospace, line numbers, Tab input, **Ctrl+S** to save, unsaved indicator). **Reload / Save**. Success shows `Saved AGENTS.md`; failures show the error. Nothing is written if the content is unchanged.
- If the file was changed externally after loading, the save is not applied and returns **409** (prompting a Reload). CRLF files are saved with CRLF. Non-UTF-8 files and files over 1 MiB are rejected.
- **Git status** (`M AGENTS.md` / Untracked, etc.) and **View Diff** (equivalent to `git diff HEAD -- AGENTS.md`; an untracked file is shown as all lines added). **It does not commit** (Commit is separate).
- **Nested AGENTS.md**: `AGENTS.md` files in subdirectories of the repository (except those ignored by `.gitignore`) can be chosen and edited with the **File** selector.
- The dashboard's repository row shows `Git: clean` / `AGENTS.md: found`.
- Only files named `AGENTS.md` inside the repository (or worktree) can be written (`..`, absolute paths and symlinks leading outside the repository are rejected).

## Task Dependencies

Choose **After other tasks complete** under **Run** in New Task and pick the tasks to wait for in **Depends on**; the task starts automatically once all of them have execution status `completed` and semantic outcome `success`
(A, B, C → D. The policy is `all_success`; the design allows adding things like `all_terminal` later).

- **The worktree is created just before running.** A waiting task holds only a branch name and a worktree path, with no worktree (no useless worktrees are created).
  The base ref is checked for existence at creation, and the task branches from **the ref at start time**.
- States: `waiting_dependencies` (waiting; the dashboard shows `Waiting (2/3 complete)`) → `queued` (ready, not yet claimed by a runner) → `starting` → `running`.
- If a dependency becomes `failed` / `stopped` / `blocked`, this task becomes **`blocked`** (`Dependency B failed`) and is not run on its own.
  Proceed with **Run Anyway** (start ignoring dependencies) or **Retry Failed Dependency** (rerun the failed dependency in the same worktree and thread, then wait again) on the detail page.
- While a dependency is **being retried (`retry_wait`), the task keeps waiting**. A transient failure alone does not make it `blocked`; it becomes `blocked` once the dependency finally reaches `failed`.
  Dependencies in `waiting-for-quota` / `interrupted` are also resumable pauses, so the task keeps waiting.
- Stopping a dependency makes it `stopped`, so tasks depending on it become `blocked`.
- Dependencies form a DAG. **Self-dependencies, duplicates and cycles are rejected** (the same check applies when replacing the dependencies of a not-yet-started task with `PUT /api/tasks/{id}/dependencies`).
- How double starts are prevented: `waiting_dependencies → queued` is **a single conditional UPDATE** that includes the "all dependencies completed with success" check, and `queued → run` is
  **a single UPDATE conditioned on `claimed_by IS NULL` (the claim)**. Even if parents finish at the same moment and the listener, scheduler and API evaluate repeatedly, the task can start only once.

### Task dependency graph

Open **依存グラフ** from the task list (`/dependencies`). The graph reads existing SQLite task dependencies and scheduled-instruction **Depends on** settings, with arrows from prerequisites to successors. Tasks without settings remain independent. It makes no AI calls and infers no relationships from instructions or names. Scheduled arrows describe follow-up turns, separately from initial-task prerequisites. Multiple settings between the same tasks share an arrow whose detail lists every setting. Cancelled schedules are excluded; completed/failed schedules carry their saved status.

Select cards or arrows to see full titles, repositories and links to the existing schedule screen. Change dependencies there by cancelling and replacing reservations. Pan, zoom, fit, horizontal/vertical layout, search and repository filters are available. **このタスクだけ表示** (or double-clicking a card) narrows the graph to that task's prerequisites and successors (all, or direct only via the selector) and fits it to the view; **全体に戻す** restores the full graph. Drag the grip below the graph to change its height and the grip between graph and detail panel to change the panel width (arrow keys also work; double-click resets; sizes are remembered per browser); prerequisites outside the filter remain visible as contextual cards. Existing two-second polling refreshes settings and state while preserving positions and selection on status updates. Missing references and projected task-level cycles are reported without repairing settings. Scheduling and completion acceptance behavior are unchanged.

Browser fixtures use temporary data and a separate localhost port, never registered tasks or Codex. Set `GRAPH_FIXTURE_HOME` to a fresh temporary directory and run `uvicorn graph_browser_app:create_fixture --factory --host 127.0.0.1 --port 8766` with the repository and `tests` on `PYTHONPATH`. Run `node tests/graph_browser_smoke.cjs` with `GRAPH_BROWSER_BASE=http://127.0.0.1:8766`, `GRAPH_PLAYWRIGHT_MODULE` pointing to an installed Playwright module, `GRAPH_BROWSER_EXECUTABLE` pointing to a Chromium executable, and `GRAPH_SCREENSHOT_DIR` pointing to an existing temporary directory.


## Scheduled Instructions

A **scheduled instruction** is a follow-up turn for a task's **existing Codex thread** that is held back until other tasks have finished. It is not a task dependency: the target task is never made to wait,
and nothing here starts a task. Use it for "when A and B are done, merge their results into X and run all the tests".

In **Additional instruction** on the detail page, write the instruction and use the **Schedule** block under it (the immediate **Send Standard / Send Fast** buttons stay as they are):

- **Delivery**: *Send when thread is idle* (no dependencies; it queues behind the current turn) or *Send after tasks complete* (tick the tasks under **Depends on**).
- **Speed**: *Standard* (`default`) or *Fast* (`priority`), stored **per instruction**. It never follows the task's tier or the previous turn: a Standard reservation after a Fast turn is Standard.
  Reasoning effort and auto approval come from the task; what a turn actually used is recorded on the turn and the attempt.
- **Schedule Instruction** reserves it. Task Detail then lists **Scheduled instructions** (`#1 WAITING · After: ✓ P14 … P15 running`, `WAITING FOR THREAD`, `READY`, `RUNNING`, `COMPLETED`, `BLOCKED`, `CANCELLED`, `FAILED`) with **Cancel**.
  The dashboard only adds a small `Scheduled: 3 · Ready: 1` under the task name.

It is sent only when **both** hold: every selected task is `completed` with outcome `success`, and the target thread is idle (its task is `completed`: not running, queued, retrying, failed, stopped, interrupted or waiting for quota).
It always continues the same `codex_thread_id`, worktree and branch (`codex exec resume <thread>` / a new turn on the same app-server thread); no session is created, so the prompt cache is kept.

| Status | Meaning |
| --- | --- |
| `waiting_dependencies` | at least one selected task has not completed with `success` (includes semantic issues and resumable execution pauses) |
| `waiting_thread` | all dependencies are done, but the thread is busy |
| `ready` | everything is satisfied; waiting for its turn in the queue of the thread |
| `running` | claimed and sent; it ends with the turn (`completed` on semantic success, otherwise `failed` for an execution or semantic issue) |
| `blocked` | a dependency ended `failed` / `stopped` / `blocked`, or the target worktree was deleted / has no thread (stays blocked; Cancel or create a new one) |
| `cancelled` | cancelled before it was sent: it is never sent |

- **Several instructions per thread** are sent **one at a time, oldest first** (`created_at`, then `id`); a younger one whose dependencies are done may go ahead of an older one that still waits for its dependencies.
- **No double send.** Every transition is one conditional `UPDATE` (the status it leaves and the facts that justify it are in the `WHERE`). The claim `ready → running` is **one transaction** that also turns the target task
  `completed → queued` with the turn as its pending turn, and is only taken if nothing of that task is `running` or ahead of it. A second evaluator, a second dependency finishing at the same moment, a manual Send, or a Cancel
  can therefore never win the same instruction (or the same thread) twice; if either UPDATE does not match, both are rolled back.
- **Unexpected stops are the task's business.** Once running, an instruction is never re-sent. If Codex dies during that turn, the task's automatic recovery resumes the same thread and worktree from their current state
  (a short "check the current state" prompt, not the instruction again) and the instruction stays `running` until the task ends. The same after a GUI restart: waiting instructions are re-evaluated from the database, a `running` one is left to recovery.
- Self-dependency (the target task as its own dependency), duplicates and unknown tasks are rejected. API: `GET/POST /api/tasks/{id}/scheduled`, `DELETE /api/tasks/{id}/scheduled/{sid}`.
- Tables: `scheduled_instructions` (`id, task_id, prompt, status, service_tier, created_at, ready_at, started_at, finished_at, blocked_reason, created_by`) and `scheduled_instruction_dependencies`
  (`scheduled_instruction_id, depends_on_task_id`, `UNIQUE` together).

## Automatic Recovery

Unexpected stops (the Codex child process died, the app-server died, a transient connection / I/O error, an unexplained failure) are by default resumed automatically **up to 3 times** (**Auto recovery** in New Task; changeable per task). Resuming uses **the same task, worktree, branch and Codex thread**.

| Class | Examples | Behaviour |
| --- | --- | --- |
| retryable | process killed by a signal / app-server exit or timeout / `httpConnectionFailed` / `responseStreamDisconnected` / `serverOverloaded` / `internalServerError` / EAGAIN, etc. | Retry |
| unknown | failures that cannot be classified | **Retry** (the limit is always respected) |
| quota | `usageLimitExceeded` / `rateLimitExceeded` / rate limit | No retry; `waiting-for-quota` (does not consume the retry count; resume manually; no API billing or fallback to another model) |
| non-retryable | **user Stop** / authentication / configuration / invalid model / invalid arguments / invalid Git repository / permanent worktree-creation error / dependency failure / worktree missing / thread does not exist / context window exceeded / limit reached | `stopped` / `failed` (no retry) |

- **Intervals:** 10 s → 30 s → 60 s (`CODEX_GUI_RETRY_BACKOFF`). No back-to-back retries. While waiting it shows `Retry 1/3 in 18s`.
- **How it resumes:** The original instruction is not resent; a fixed short recovery instruction is sent to the same thread. Codex checks the current state (`git status` / `git log` and the conversation) and does not redo finished work
  (no duplicate commits / pushes).
  It was confirmed on a real machine that Codex 0.159.2 has **no** feature to automatically continue an interrupted turn (`codex exec resume <id>` requires a prompt).
- **If it died before the turn started** (before Codex returned `turn/started`), the instruction may not have reached the thread, so instead of the recovery instruction **the original instruction is sent again** to the same thread
  (nothing had run yet, so nothing is executed twice). If there is no thread id yet, a new thread is created in the same worktree and the log records
  `Retry started a new Codex thread because no previous thread ID existed.`
- **Git safety:** Before resuming, the worktree's existence is checked (**if it is gone, it is not recreated and the task becomes `failed`**). `git status` / HEAD / push state are **only recorded**; `reset` / `clean` / `checkout` are never run.
  Codex looks at the current state and continues any partial changes. The GUI never rewrites history.
- **Stop** is an explicit action, so the task becomes `stopped` and is not retried (dependent tasks become `blocked`).
- **Manual Retry** (`failed` / `stopped`): reruns in the same worktree and thread. If the automatic retry limit has been exceeded, a confirmation dialog appears. Use **Start New Session** for a different thread.
  During `retry_wait`, **Retry Now** and **Disable Auto Retry** let you cancel the pending retry.
- **History:** The `task_attempts` table records each run (first run / follow-up / automatic or manual retry / recovery after a GUI restart): start, end, exit code, result, failure type, thread id, whether it was a resume, service tier,
  reasoning effort and the git state before resuming (separate from the token-usage `turns` table). They are listed under Recovery on the detail page.

### When the GUI itself goes down

On startup, tasks in `running` / `starting` / `retry_wait` / `queued` are reviewed.

- `running` / `starting`: if the recorded **process is still alive** (pid, **start time** and command line all match; a reused pid counts as a different process) it is left alone and monitored, and retried when it ends.
  If it is gone, the task becomes `retry_wait` when automatic retry is enabled and within the limit, otherwise `failed`. **Restarting the GUI alone never causes the same task to run twice.**
- `retry_wait`: the timer lives in the DB, so it continues (`failed` if the worktree is gone).
- `queued` (claimed but not started): the claim is released and the scheduler starts it.
- A task stopped by a normal exit (Ctrl+C) is `interrupted` as before (continue with Send / Resume interrupted).
- Premise: **one GUI process per DB** (startup recovery takes over the previous process's claims).

## Git Worktree Handling

- A worktree is not removed when you Stop, so you can inspect the work in progress.
- **Delete Worktree** is available only for finished tasks (completed / failed / stopped / interrupted). A confirmation dialog appears if there are uncommitted changes or untracked files.
- Deleting a worktree **leaves the branch** (and its commits). Delete the branch separately with **Delete Branch** (reconfirmed if it is unmerged).
- Branch names are `codex-gui/<task-id>-<slug>`.

## Restarting the GUI

- History stays in SQLite.
- On a **normal exit** (Ctrl+C) of the GUI, running turns are stopped and become `interrupted`, and the app-server exits too. **The thread remains on the Codex side**, so after a restart you can continue the same thread with Send.
- For recovery after a forced kill of the GUI, see "When the GUI itself goes down" under [Automatic Recovery](#automatic-recovery) (a surviving process is monitored; otherwise automatic retry applies).

## Auto Approval and Network Access

Network access (ON by default) passes `sandbox_workspace_write.network_access=true` as thread config on every turn (it was confirmed that the app-server's `thread/start` returns `networkAccess: true`).
While ON, Codex can reach any external host from inside the sandbox (instructions hidden in a prompt can more easily leak code or secrets). Turn it OFF for untrusted repositories or instructions.

Auto approval (ON by default) is `approvalPolicy: "on-request"` + `approvalsReviewer: "auto_review"` + `sandbox: "workspace-write"` on the app-server, and
`codex exec --approve-for-me` on the `exec` backend (in both cases approval requests inside the workspace-write sandbox go to automatic review).
When OFF it is `approvalPolicy: "never"` (everything must complete inside the sandbox; operations needing escalation fail, since the GUI has no approval dialog).
`--dangerously-bypass-approvals-and-sandbox` and `danger-full-access` are never used (this is verified by tests).

Because of the sandbox, Codex itself may be unable to write to the `.git` outside the worktree. In that case, commit with the GUI's **Commit** button.

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_GUI_HOME` | `~/.local/share/codex-gui` | Data directory |
| `CODEX_BIN` | `codex` | codex executable |
| `CODEX_GUI_MAX_CONCURRENT` | `0` (unlimited) | Concurrent runs. Excess tasks wait as `queued` (never changed automatically from the usage limit) |
| `CODEX_GUI_BACKEND` | `app-server` | `app-server` (thread / turn) or `exec` (legacy `codex exec`) |
| `CODEX_GUI_SUBSCRIPTION_ONLY` | `1` | `1`: run only with a ChatGPT login, and strip API-key variables from codex's environment |
| `CODEX_GUI_CONTEXT_WARN_PERCENT` | `80` | Context usage (%) at which Context Guard warns |
| `CODEX_GUI_PREFERRED_MODEL` | `gpt-6.1-sol` | Preferred model. Falls back to "Codex default" if it is not in `codex debug models` |
| `CODEX_GUI_HOST` / `CODEX_GUI_PORT` | `127.0.0.1` / `8765` | Listen address |
| `CODEX_GUI_AUTO_RETRY` / `CODEX_GUI_MAX_RETRIES` | `1` / `3` | Defaults for automatic retry after unexpected stops (changeable per task) |
| `CODEX_GUI_RETRY_BACKOFF` | `10,30,60` | Seconds to wait before retries (comma-separated; the last value is reused afterwards) |
| `CODEX_GUI_SCHEDULER_INTERVAL` | `2` | Interval (seconds) for checking waiting / retry-waiting tasks |
| `CODEX_GUI_SSH_KEY` | `~/.ssh/id_ed25519` | Key to `ssh-add` when the agent is empty |
| `CODEX_GUI_SSH_AGENT_ENV` | `~/.ssh/agent.env` | Where the agent's environment variables are saved |
| `CODEX_GUI_SSH_DIAGNOSTICS` | `1` | `0` disables the SSH diagnostic log at startup |
| `CODEX_GUI_SSH_GITHUB_TEST` | `1` | `0` disables `ssh -T git@github.com` at startup |

## SSH Authentication

`run.sh` manages an `ssh-agent` on WSL so that Push (the GUI's Push button) and git operations inside Codex can reach SSH remotes (`git@github.com:...`).

### How it works

On startup, `./run.sh` picks an agent in this order:

1. If the agent at `SSH_AUTH_SOCK` in the environment responds, use it
2. Read `~/.ssh/agent.env` (the `SSH_AUTH_SOCK` / `SSH_AGENT_PID` of the previously started agent) and reuse it if it responds
3. Only if neither works, start a new `ssh-agent` and save it to `~/.ssh/agent.env` (mode 600)

It then checks the keys with `ssh-add -l` and, **if none is registered**, runs `ssh-add ~/.ssh/id_ed25519`.
For a key with a passphrase, enter it at the normal `ssh-add` prompt (start `run.sh` from a terminal).

`SSH_AUTH_SOCK` stays exported and is inherited uvicorn → FastAPI → Codex / git subprocesses.
The agent is not started again for each Codex task (only once, when `run.sh` starts).
Even if you launch uvicorn directly without `run.sh`, subprocesses are given the socket from `~/.ssh/agent.env` if it is alive.

To use the same agent in other shells, run `. ~/.ssh/agent.env`.

### Diagnostic log at startup

On startup the GUI logs the following (to the terminal where you ran `run.sh`):

- `ssh-add -l` (exit code 0 = keys present / 1 = agent present but no keys / 2 = cannot connect to the agent)
- `git config --get remote.origin.url` (for the GUI's launch directory and recently used repositories)
- `ssh -T git@github.com` if the remote is a GitHub SSH URL

GitHub returns **exit code 1** even when `ssh -T` succeeds. So success is judged not by the exit code but by whether the output contains
`successfully authenticated`. Diagnostics run asynchronously and do not delay startup.

### Troubleshooting

| Symptom | Check / fix |
| --- | --- |
| `ssh-add -l (exit 2)` in the log | Cannot connect to an agent. Restart with `./run.sh`. Deleting `~/.ssh/agent.env` and restarting also works |
| `ssh-add -l (exit 1)` / `no keys` | `ssh-add ~/.ssh/id_ed25519`. For a different key, set `CODEX_GUI_SSH_KEY` |
| `ssh-add` fails because it cannot ask for the passphrase | Run `./run.sh` from a terminal, or run `ssh-add` manually beforehand |
| `ssh -T` gives `Permission denied (publickey)` | The agent's key is not registered with GitHub. Compare the public key from `ssh-add -l` with <https://github.com/settings/keys> |
| `Host key verification failed` | First connection. Run `ssh -T git@github.com` once in a terminal to accept the host key |
| `Could not resolve hostname` / timeout | Network, DNS or proxy problem (check WSL's DNS) |
| Remote is `https://` | SSH authentication is not used. `git remote set-url origin git@github.com:OWNER/REPO.git` |
| Cannot push from inside Codex's sandbox | Tasks with **Network access** OFF, and old tasks (created before that option existed), have the network blocked. Even when ON, the sandbox may block the SSH agent socket. Use the GUI's **Commit / Push** buttons (run from the GUI process) |
| A stale agent is left over | `pkill -f 'ssh-agent -s'` also stops other agents, so stop it with `kill $SSH_AGENT_PID` (the pid in `~/.ssh/agent.env`) |

Manual check: `. ~/.ssh/agent.env && ssh-add -l && ssh -T git@github.com`

## Data Location

```text
$CODEX_GUI_HOME/
├── codex-gui.db              Task history, per-turn usage, usage-limit history (SQLite)
├── instructions.md           (optional) common working guidelines; the default text is used if absent
├── logs/<task-id>.jsonl      Per-task log (raw stdout events, non-JSON lines, stderr, system)
└── worktrees/<repo>/<task-id>/
```

Branch names are `codex-gui/<task-id>-<slug>`.

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests do not use a real Codex. They drive `tests/fake_app_server.py` (the app-server JSON-RPC: thread / turn / cumulative usage / rate limit / steer / interrupt / compact / quota errors) and
`tests/fake_codex.py` (the legacy `codex exec` mode) through the real JSON-RPC client, subprocesses, signals and git.
Coverage: signing in to Codex (browser / device code / failure / cancel), persistence of tasks and thread ids, reuse of the same thread (resume), parsing of token / cached and cache-hit calculation, rate-limit parsing (weekly only / two windows),
state transitions on quota (no retry), defaults and sent values for model, reasoning, Standard, auto approval and cached web search, no fallback to an API key,
context-guard decisions, steer / stop / compact, AGENTS.md read / write / conflict / path validation and the separation of Repository and Task worktree,
dependencies (DAG, rejection of cycles / self / duplicates, starting exactly once even when evaluated from multiple connections, waiting while a dependency is retrying, blocked / Run Anyway),
and automatic recovery (failure classification, limits, intervals, reuse of the same thread / worktree, no retry for Stop / quota / authentication, leaving the working tree untouched, no recreation of a deleted worktree,
recovery after SIGKILL of the GUI, telling apart a reused pid).

Integration tests against a real Codex are kept separate and run only when `CODEX_GUI_REAL=1` (they use a small number of tokens; cache hit is display only, and pass / fail is decided by same thread, usage retrieval, context window and usage-limit retrieval):

```bash
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_codex.py -s
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_codex.py -s -k killed_mid_turn   # SIGKILL a real Codex mid-turn → recover in the same thread and worktree
CODEX_GUI_REAL=1 CODEX_GUI_REAL_MODEL=gpt-6.1-sol CODEX_GUI_REAL_PAUSE=15 .venv/bin/python -m pytest tests/test_real_codex.py -s -k app_server
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_ab.py -s                          # A/B on a real model (small token use)
```

## Limitations

- `Profile` (codex config profiles) cannot be selected from the GUI.
- The app-server protocol is experimental (checked with 0.159.2). `CODEX_GUI_BACKEND=exec` switches back to the legacy mode.
- The effect of compact (how much the context shrinks) has only been verified on a real machine with small threads (a ~13k thread did not shrink).
- The `exec` backend has no usage limits, context retrieval, quota detection, steer or compact.
- Adaptive reasoning only suggests; it never escalates automatically.
- The context size right after a compact is unknown until the next turn.
- Usage-limit percentages are coarse integers, and per-task consumption cannot be separated during parallel runs.
- The GUI itself has no login (authentication). Signing in to Codex is for a ChatGPT account only (no API key, no sign-out action).

## Project Layout

```text
app/
  main.py           App creation, startup recovery, shutdown handling
  codex_login.py    Signing in to Codex (ChatGPT) (app-server account/login/*)
  routes.py         HTML pages and JSON API
  task_manager.py   Task creation, follow-ups (resume / steer), parallel runs, Stop, compact, recovery, Git operations, usage / limit recording
  appserver.py      JSON-RPC client for codex app-server (shared process, notification routing, environment without API keys)
  notifications.py  app-server notifications → task log
  codex_runner.py   `codex exec` mode: command building, event parsing, settings (task_config / approval_params)
  usage.py          Token usage, per-turn deltas, cache hit rate, context checks, rate-limit parsing
  instructions.py   Common working guidelines (a constant passed once when a thread starts)
  agents_md.py      AGENTS.md read / write, Git status, diff, nested discovery
  logstore.py       Per-task JSONL log writing / incremental reading
  git_manager.py    git CLI wrapper
  database.py       SQLite
  models.py         Status transitions and naming rules
  config.py         Settings
  ssh_agent.py      SSH_AUTH_SOCK inheritance for subprocesses, startup SSH diagnostics
  scheduler.py      Background loop that advances waiting / queued / retry-waiting tasks
  recovery.py       Classification of unexpected stops and retry intervals
  efficiency.py     Estimates of savings from cache and model choice (credit / API-equivalent estimates; not actual savings)
  cache_health.py   Per-turn cache-hit recording, likely causes of misses, compact monitoring
  ctx_config.py / ctx_guard.py / ctx_manager.py / turn_observer.py   Context Efficiency (settings, guard, turn observation)
  agents_audit.py   AGENTS.md health check (read-only)
  tool_probe.py / fake_responses.py   Point a real codex at a fake Responses API to measure things such as the tool list
  procinfo.py / tokens.py             pid identity checks (/proc), token estimation
docs/               Research notes on Codex CLI / app-server
static/ templates/  UI (plain HTML/CSS/JS, polling)
tests/
```

## License

[MIT](LICENSE)
