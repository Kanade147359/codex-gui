# Build artifact GC (P0)

Open **Storage** (`/storage`) to inspect build caches, change retention, run a Dry Run,
or review previous runs. Retention defaults to **5 days** and deletion defaults to
**disabled**. The existing scheduler runs a scan at startup if due, then at most
once per 24 hours (a manual run also resets that interval). When deletion is off,
these scheduled runs are Dry Runs. Enabling deletion explicitly allows scheduled
and manual deletion; the manual GC button stays disabled otherwise.

## Read-only production analysis

Run from this checkout, without starting a development GUI or using its HTTP API:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m app.artifact_gc \
  --home "$HOME/.local/share/codex-gui" \
  --retention-days 5 \
  --progress \
  --output /tmp/codex-gui-build-artifacts.json
```

Use the actual `CODEX_GUI_HOME` if different. The command opens the existing DB
with `mode=ro`, never constructs `Database`/`TaskManager`, never creates a lock or
migrates the DB, and has **no deletion option**. The JSON report includes every
found cache's path, allocated bytes, last use, owning tasks, nested Git worktrees,
candidate flag and exclusion codes. The console prints the counts/capacities and
exclusion totals when `--output` is supplied. Output must be outside the data
home and cannot overwrite a symlink. The UI's Dry Run additionally saves its
audit record in the normal DB; neither form modifies cache contents.

Run analysis in the host PID namespace. Inside a Codex sandbox or a container
without a recognizable init process, process visibility is incomplete and
`process_inspection_unavailable` excludes candidates. An empty process list is
never accepted as proof of inactivity. No running production GUI needs to be
restarted for this read-only analysis.

Hardened same-user processes can also deny `/proc` inspection on the host. Such
failures exclude collection rather than treating an inaccessible process as idle.

## What can be collected

- Only tasks registered in the DB, below `CODEX_GUI_HOME/worktrees`, are searched.
  Discovery walks nested directories and registered Git worktrees, including
  worktrees inside otherwise unknown `build/` directories.
- `target/` requires a local `Cargo.toml`, `.rustc_info.json`, and a Rust profile's
  `deps/` directory (including cross-compilation profiles).
- `build/` requires a local `CMakeCache.txt` proving both the source directory and
  build directory, plus `CMakeFiles/`. Unknown build systems are reported and
  excluded. Nested source project manifests disqualify the enclosing build.
- Git must recognize each enclosing worktree. Caches must be ignored by Git,
  contain no tracked or nonignored untracked files, and contain no Git metadata,
  logs or databases. Dirty source files elsewhere in a worktree are preserved;
  Git state is inspected without refreshing/writing its index or running fsmonitor.
- `required_paths` / `required_changed_paths` in completion contracts are protected
  along with their enclosing caches. Place `.codex-gui-keep` in a cache or any
  ancestor within its task worktree to explicitly preserve it.

Only `completed`, `failed`, `stopped` or `blocked` owners can qualify. Running,
starting, queued, retrying, dependency-waiting, interrupted and quota-waiting owners,
Needs Review, completion checks, and pending scheduled instructions protect their
caches. All overlapping registered owners are checked. Historical PID/claim fields
on finished tasks are not mistaken for live processes.

Last use is the latest regular-file access, modification or change time, or the
owner task's start, last turn or finish time. Directory creation/mtime is never
used as age evidence. Empty caches, missing/invalid finish times, inspection errors
and unknown process visibility are excluded. File metadata updates can extend
retention conservatively. A cache must have no use within the retention period.

Symlink paths/components/contents, mounts, special files, custom Cargo target
configuration, references from other managed paths and shared caches are excluded.
Hardlinks are allowed only when every link to each inode is confined to that same
cache. Disk usage counts an inode once, including across overlapping scan entries;
individual entries can overlap, so their sizes must not be summed independently.
Symlink destinations and skipped mounts are not traversed. `size_complete=false`
means the displayed bytes are a lower bound.

## Coordination and persistence

Deletion holds an exclusive `flock` gate shared with the existing `_dispatch`
claim/launch path. Dispatch takes a nonblocking shared lock; tasks queued during
collection stay queued until the scheduler can dispatch them. Already claimed
jobs have protected active/queued DB states before the lock is released. A separate
collection gate prevents duplicate manual/daily runs without blocking dispatch
for Dry Runs. These gates apply to GUI processes running this version; the
standalone CLI is intentionally only an uncoordinated, read-only snapshot.

Immediately before each removal the collector refreshes owners/scheduled work,
processes (cwd, executable, open FDs, mappings and Cargo target environment), shared
references, Git state, cache identity and file times. Unknown state fails closed.
Removal is anchored with directory descriptors, `O_NOFOLLOW`, inode identity and
Python's symlink-resistant `rmtree`; Python 3.10 uses a Linux `/proc/self/fd`
parent anchor. No Git worktree or branch operation is performed.

Settings reuse `app_settings` (`artifact_gc.retention_days`,
`artifact_gc.auto_delete`). The idempotently created `artifact_gc_runs` table
records mode, trigger, start/end, status, full report and released bytes. An
unfinished previous run is marked interrupted by the next run. Partial removal
errors are recorded, with `released_bytes_incomplete=true` rather than claiming
an exact freed total. Shutdown drains the GC worker before closing the DB.
Sources, task history, branch, Git registration and Codex thread metadata are
never changed by GC; resumed tasks can regenerate caches in the same worktree.

## Validation

```sh
python -m pytest -q tests/test_artifact_gc.py
CODEX_GUI_BROWSER=1 python -m pytest -q tests/test_ui_storage_browser.py
```

Both use isolated temporary repositories, worktrees and DBs, fake Codex, and
an ephemeral localhost browser-test port. The tests cover four/six-day retention,
file access and task use, nested worktrees, protected states, symlinks and ancestor
replacement, internal/external hardlinks, explicit artifacts, CMake proof, real
process detection, a real worker/dispatch race, daily idempotence, audit persistence,
read-only CLI behavior, and continuation in the same thread after actual GC of
temporary test caches. No production deletion is part of validation.
