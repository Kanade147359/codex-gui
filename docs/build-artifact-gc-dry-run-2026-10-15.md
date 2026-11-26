# Production Dry Run: 2026-10-15

Snapshot start (UTC): `2026-10-15T10:16:53.433713+00:00`. Retention: **5 days**.

This was a read-only CLI analysis in the host PID namespace. The production GUI
was not restarted, its DB was opened with `mode=ro`, and no cache was deleted.

| Measure | Result |
| --- | ---: |
| Detected directories | 331 |
| Detected regular-file allocation (unique inodes) | 388,339,097,600 bytes (361.67 GiB) |
| Verified candidates | 0 |
| Verified candidate capacity | 0 bytes (0 GiB) |
| Released | 0 bytes |
| Otherwise qualified, but process inspection incomplete | 7 directories / 1,121,910,784 bytes (1.04 GiB) |

The last row is **not approved reclaimable capacity**. Inaccessible same-user
process details prevented confirmation that these caches were idle. GC fails
closed; running this inside a Codex PID namespace also cannot establish host
idleness. The report excludes every unverified cache rather than assuming safety.

The supplied 676 GiB baseline is not directly comparable to this inventory:
this scan measures detected artifact trees only and counts a shared inode once,
including across overlapping entries. Source/data outside those trees, directory
metadata and symlink destinations are not included.

Exclusions can overlap; counts below must not be summed as unique directories.

| Exclusion | Directories |
| --- | ---: |
| `needs_review` | 29 |
| `process_inspection_unavailable` | 331 |
| `protected_content` | 101 |
| `referenced_or_shared_directory` | 144 |
| `source_manifest_in_build` | 45 |
| `special_file` | 2 |
| `symlink_in_artifact` | 46 |
| `task_active_or_waiting` | 1 |
| `unconfirmed_build_directory` | 189 |
| `unconfirmed_rust_target` | 111 |
| `unknown_last_use` | 10 |
| `unknown_task_last_use` | 1 |
| `within_retention` | 114 |
| `worktree_process_active` | 1 |

Discovery errors: 0. Unknown last-use times remain protected.

The detailed per-path JSON remains local at
`/tmp/codex-gui-gc-host-production.json`; it is not uploaded to GitHub.
To repeat the analysis, use [the read-only command](build-artifact-gc.md#read-only-production-analysis).

Validation: GC tests **40 passed**; related regression suite **294 passed**;
Chromium Storage flow **1 passed** (390 px viewport, isolated localhost server).
The broader suite had **813 passed, 27 skipped, 3 failed** at its run; all three
failures were reproduced against unchanged HEAD in an isolated `/tmp` archive:

- `test_ctx_e2e.py::test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded`
- `test_ctx_e2e.py::test_real_codex_tool_output_limit_reaches_the_model`
- `test_ctx_integration.py::test_tool_output_token_limit_only_lowers_the_cap`

Those failures concern existing Codex tool-output measurement expectations. The
tests use a local fake Responses endpoint without model quota. The GC changes do
not change that behavior; the diagnostics are retained locally under
`/tmp/codex-gui-gc-{full,baseline}-tests.txt`.
