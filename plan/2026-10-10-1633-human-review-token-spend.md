---
status: approved
issue: 1633
spec: spec/2026-10-10-1633-human-review-token-spend.md
---

# Plan: kubejob builds must report the tokens they spent

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1633`, branch
`fix/1633-human-review-token-spend`, base `origin/dev`. The spec was approved
at `f694bb37`. All paths are repo-relative. Line numbers are against
`origin/dev` `ef25678b`, which is what the branch is after the Step 0 rebase
(#1670 `2eba9dbe` and #1677 `ef25678b` merged after `f694bb37`). They are not
against the spec's base `d56073c0`, which is not an ancestor of HEAD.
Against `f694bb37`, #1670 moves `agent_kubejob.py` by -7 before line 625 and
by -10 after it, and #1677 moves `build_backend.py` by +16 past line 1400.
`agent_service.py`, `completion.py`, `cli/*` and `core/workspace_fetch.py`
do not move.

## Approved decisions (self-contained)

- **D0.** #1633 is not fixed on dev. The gaps below were verified at `f694bb37`.
- **D1. Scope.** Fix the AIFactory kubejob gaps only. Two follow-up issues are
  drafted: one for TFactory (usage is reported only at a terminal outcome) and
  one for CFactory ("no model called" vs "nothing reported yet"). Neither
  blocks this PR. The event envelope, endpoint, headers and secrets stay
  unchanged.
- **D2. One helper reports spend.** Stopped, reaped, abandoned and failed
  builds all go through `AgentService._report_kubejob_usage(job_id, status)`.
  It sends a snapshot only when `maybe_fetch_usage` returns True, meaning it
  wrote new, higher spend locally. Otherwise it sends nothing. It never sends
  a zero or empty block.
- **D3. No re-send after review.** A resumed build does not re-send after
  review; that is a follow-up issue. The `.terminal_completion_emitted` marker
  (#1407) is unchanged, and the kubejob review pause (C4) must not write it.
- **D4.** Go ahead on the code findings. MyFriends logs are optional.
- **D5. Gate.** Coding starts after #1670 (PR #1694) merges, then the branch
  is rebased on dev. Do not wait for #1677. #1669 has already merged
  (`2cf18708`). #1670 merged as `2eba9dbe` and #1677 as `ef25678b`, so
  Step 0 is now just the rebase plus a re-check of the line ranges.
- **C1. The Job always pushes usage on exit.** In `cli/main.py`,
  `handle_build_command` is wrapped in `try/finally`:
  - The `finally` always calls `maybe_push_usage`.
  - It calls `maybe_push_plan` only when not `args.stop_after_planning`.
  - Both calls are removed from the success block, so nothing is pushed
    twice.
  - The branch, memory, gate-marker and task_logs pushes stay success-only,
    so a failed branch is never pushed.
  - Every `sys.exit` therefore pushes usage, and the exit code is preserved
    because the helpers never raise.
  - Planning-only Jobs now push usage too.
  - Crashed, OOM-killed or SIGKILLed Jobs are out of scope; the #1249 live
    snapshots cover them.
- **C2. What the helper does.**
  - Split `job_id`, then call `resolve_project_path` and `spec_dir_for`.
  - Run `maybe_fetch_usage` in `asyncio.to_thread`. Call
    `emit_usage_snapshot(spec_dir, task_id=job_id, project_id, spec_id, status)`
    only when the fetch returned True.
  - Catch every exception and log at debug level, with `sanitize_log(job_id)`
    only.
  - The "new data" gate is what prevents a duplicate `failed` event, because
    CFactory dedupes by event id, not by status.
  - Call sites, all with status `failed`: `_on_kubejob_build_failed`,
    `_stop_kubejob_build` and `reap_abandoned_tasks`.
- **Decision 4. Stop reports `failed`.** A stopped build sends a `failed`
  snapshot, not `human_review`, even though the stop path sends task status
  `human_review/errors`. Stop must leave the control status alone.
- **C3. `maybe_fetch_usage` writes only higher totals.**
  - Drop the `dest.is_file()` early return and always GET the object.
  - Skip the object when it is over 1 MiB, not JSON, or not a dict, logging
    only its size.
  - Write atomically (temp file, then `os.replace`) only when the local copy
    is missing, unreadable, or has a strictly lower `totalTokens`.
  - Return True only when a write happened.
  - With no object store, catch the `RuntimeError`, keep the local file and
    return False.
  - A rerun that really starts from zero is never sent (fail toward sending
    nothing). A test pins this.
- **C4. A review pause in the Job is not a completion.** In
  `_on_kubejob_build_done`, run `maybe_fetch_plan` in a thread, then read
  `implementation_plan.json`.
  - The build counts as paused only when `status == "human_review"` and
    `reviewReason` is `plan_review` or `injection_scan` (an allowlist).
  - QA's `human_review/completed` and `human_review/qa_issues` keep today's
    path, including the handoff.
  - On a pause, call
    `task_control.write_control(spec_dir, status="human_review", review_reason=<reason>, updated_by="kubejob_review_pause")`,
    then `await _report_kubejob_usage(job_id, "human_review")`.
  - A pause skips `_emit_kubejob_terminal_completion` and
    `_record_kubejob_terminal`. That means no handoff, no endgame, no #1407
    marker and no `_update_plan_status("human_review")`.
  - Credential release, `_report_orphaned_worktrees` and `_drain_queue`
    still run.
  - `terminal_status="completed"` in `_emit_kubejob_terminal_completion` is
    unchanged.
- **Decision 5. The Job writes the pre-flight approval pause.**
  - Where: the `auto_continue` branch of the pre-flight review check, only
    when `WORKSPACE_URI` is set.
  - What: before `sys.exit(0)`, write `implementation_plan.json` with
    `status="human_review"` and `reviewReason="plan_review"`. If no plan
    exists, write a `{"phases": []}` skeleton, following the pattern in
    `agents/coder.py:1160-1175`.
  - C1's `finally` then pushes it. The subprocess path is unchanged.
- **C5. Review snapshot before the ownership check.** In the exit handler,
  move the `lifecycle == "review"` `emit_usage_snapshot` block above the
  #1628 `if await self._k8s_job_owns(task_id): return`. `mark_terminal` stays
  below the return, so the #1628 guard still holds. This is a pure move.
- **C6. Treat `token_usage.json` as untrusted.** This applies in
  `usage_from_aggregate` and `_worker_records`.
  - Missing or None fields still count as 0.
  - A private `_num(v, cap)` raises a private `_BadUsage` for bool,
    non-int/float, NaN, ±inf, negatives and values over the cap.
  - Caps: tokens 1e12, cost 1e6 USD, `duration_ms` 1e10. Integer fields
    stay int.
  - **Decision 1:** over-cap values are rejected, not clamped.
    `usage_from_aggregate` catches `_BadUsage` and returns None, so no usage
    block is sent at all.
  - String fields (`model`, `provider`, `worker_id`, `phase`, `subtask_id`,
    `routing_tier`) become `str[:128]`, or None when not a str.
  - `workers` keeps only the first 256 entries in sorted order.
  - A valid block must stay byte-identical to today's.
  - A bad file must still send the terminal event (without usage) and write
    the marker.
  - Tenant and project never come from the file.
- **C7 / Decision 2. Worker metrics once per terminal event.**
  - Move `_emit_worker_metrics(workers)` out of `usage_from_aggregate` and
    into `emit_terminal_completion`, so it runs once per terminal event,
    under the marker.
  - Live per-worker OTel stops updating while a build runs.
  - Known caveat 1: the marker is written only after a confirmed delivery,
    so an undelivered terminal event that retries re-emits metrics.
  - Known caveat 2: `routes/tasks.py:474` also calls
    `emit_terminal_completion` outside the marker.
  - Both caveats go in the PR body; neither is fixed here.
- **Decision 3. Out of scope.** No SIGTERM handler. No change to the #1407
  marker, the event envelope, CFactory or TFactory.

## Facts verified at `ef25678b` (the rebased branch)

- **Line ranges.** The ranges below match the rebased tree in `cli/main.py`,
  `core/workspace_fetch.py`, `cli/build_commands.py` (371-380),
  `agent_kubejob.py` (133-166, 190-224, 821, 879, 1006-1052) and
  `completion.py` (210, 389, 432, 453, 960, 1003, 1040).
- **`agent_service.py`.** The #1628 comment is at 366-373, the
  `_k8s_job_owns` return at 374-375 and the `mark_terminal` try at 376-382.
  The running-cost comment is at 383-387 and the review block at 388-406.
- **The whole plan was applied in a scratch worktree on `ef25678b`.** Both
  strict ratchets passed (ruff: 6 unchanged; mypy: 1 improved, 5
  unchanged). Default ruff, `gen_autonomy_matrix.py --check` and
  `security_lint.py` were clean. `tests/` (`-m "not slow"`),
  `apps/backend` and `apps/web-server/tests` had 0 failures. Getting there
  needed the traps added to Steps 1-4 (PLR0402, PTH105/PTH108, the `block`
  annotation, the `_Recorder` host and the `cli.main` import).
- **Test locations.** All six existing test files are in root `tests/`, not
  `apps/web-server/tests`. That puts them outside the strict ratchet; only
  ruff applies to them.
- **C4 always sees the pushed plan.** `maybe_fetch_plan`
  (`workspace_fetch.py:378`) already overwrites the local plan every time.
- **Review reasons.** Both `plan_review` and `injection_scan` exist as
  `reviewReason` values. `injection_scan` is written by
  `agents/coder.py:1160-1175`.
- **Autonomy matrix.** `scripts/gen_autonomy_matrix.py` cites none of the
  touched files.
- **Planning-only Jobs do not hit the pre-flight pause today.**
  `build_backend.py:718` passes `--force` when `should_pass_force(spec, force)`
  is true, and that is `force or not review-required`
  (`task_phase.py:139-152`). The only caller that sets
  `stop_after_planning=True` is `delegation_runner.py:83-84`, and it also
  passes `force=True`. So no planning-only Job reaches the pre-flight check
  at `build_commands.py:316-317`, which runs before the
  `stop_after_planning` branch at 487. See open question Q-A.

## Open question (not in the spec)

- **Q-A. A planning-only Job's pause is never pushed.** A `stop_after_planning`
  Job that hits the Decision 5 pre-flight pause writes the plan, but C1's
  `finally` does not push it, so C4 never sees the pause. That build then
  takes today's path: usage is still reported via C1/C2, but the pause is
  reported as a completion. Today this cannot happen, because the only
  planning-only caller also forces (see Facts). A future caller that plans
  without `force` would hit it.
  - Default if not answered: ship as specified, and list it in the PR body
    as unreachable today. No follow-up issue is needed.
  - The alternative needs a spec change: push the plan in the `finally` also
    when Decision 5 wrote the pause.

## Steps

Handoff: Steps 1-4 edit eight files, so one `coder` agent runs Steps 1-4 in
order. It gets this plan path and Step 1, and each later step goes to the same
agent via `SendMessage`. Step 5 and the review stay with the session. The
review is done by a fresh Opus agent given only this plan path and `git diff`.

Python: the worktree has no venv. Use
`PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python` and
put its `bin` on `PATH` before committing so the pre-commit hook runs.

0. **Gate and rebase (no edits).** Run
   `git fetch && git rebase origin/dev` in the worktree.
   - Re-check every range named below in `agent_kubejob.py`,
     `agent_service.py`, `completion.py`, `cli/main.py` and
     `build_commands.py`. D5 requires the `agent_kubejob.py` ones.
   - Record any shift in this plan before coding.
   - Verify by `gh pr view 1694 --json state -q .state` (expect `MERGED`) and
     `$PY scripts/gen_autonomy_matrix.py --check` (expect no diff).
   - Traps:
     - Do not wait for #1677.
     - If the autonomy matrix check fails, regenerate the matrix; do not
       hand-merge it.
     - If `CHANGELOG.md` conflicts, keep both entries.

1. **`tests/*` (red tests, root `tests/` only).** Append the cases listed in
   "Tests" to the six existing files, and add
   `tests/test_cli_build_push_on_exit.py`. Verify by:
   - The focused pytest command in "Tests": the new cases fail and the 32
     existing cases pass.
   - `ruff format --check tests && ruff check tests`.
   - Traps:
     - Patch through the module (`core.workspace_fetch.maybe_push_usage`),
       because `cli/main.py` imports the helpers lazily.
     - Use `monkeypatch.setenv`/`delenv` for `WORKSPACE_URI`.
     - Spawn no subprocesses, or pass `env=` from `child_env()`
       (`test_no_unscrubbed_spawn`).
     - Use module imports only (`from cli import build_commands`,
       `from core import workspace_fetch as wf`,
       `from core import artifact_store as a_s`), not aliased multi-name
       imports.
     - **`cli.main`.** Get the module with
       `cli_main = importlib.import_module("cli.main")`. Do not use
       `import cli.main as cli_main` or `from cli import main`.
       `cli/__init__.py:16` re-exports the function `main`, which shadows the
       submodule, so both forms bind the function, and
       `monkeypatch.setattr(cli_main, "setup_environment", ...)` raises
       `AttributeError`.
     - **`resolve_project_path`.** Patch it in both places:
       `agent_kubejob.resolve_project_path` (module import, line 22) and
       `server.project_registry.resolve_project_path`, which
       `_emit_kubejob_terminal_completion` imports lazily.
     - **Store fake.** Build `store = a_s._fake_store()` before patching,
       then `monkeypatch.setattr(a_s, "ArtifactStore", lambda *a, **k: store)`,
       as `test_usage_round_trip` does.
     - **`graphify-out/manifest.json`.** Test runs rewrite it. Never stage
       it.
     - `tests/pytest.ini` already sets `asyncio_mode=auto`.

2. **Job side (backend): C1, Decision 5, C3.** This step edits three files.
   - **`apps/backend/cli/main.py:506-522` (C1).** Wrap the
     `handle_build_command(...)` call in `try/finally`. Import the module
     lazily, as `from core import workspace_fetch  # noqa: PLC0415`. The
     `finally` calls:
     - `maybe_push_usage(spec_dir, spec_dir.name)` always;
     - `maybe_push_plan(spec_dir, spec_dir.name)` only when not
       `args.stop_after_planning`.
   - **`apps/backend/cli/main.py:531-567` (C1).** In the
     `if not args.stop_after_planning:` block, delete the plan push (543-548)
     and the usage push (555-558) with their comments, and drop both names
     from the import at 532-540. Keep `maybe_push_workspace_branch` (542),
     `maybe_push_memory` (554), `maybe_push_gate_marker` (561-563) and
     `maybe_push_task_logs` (567) there.
   - **`apps/backend/cli/build_commands.py:371-377` (Decision 5).** Inside
     `if auto_continue:`, before `review_state.save(spec_dir)`, and only when
     `os.environ.get(WORKSPACE_URI_ENV)` is set (`core.workspace_fetch`):
     - Load `spec_dir / "implementation_plan.json"`, or `{"phases": []}` if
       it is missing, unreadable, or not a dict.
     - Set `status="human_review"` and `reviewReason="plan_review"`, and
       write the file.
     - Put this in a small module-level helper
       (`_record_preflight_pause(spec_dir: Path) -> None`). Add `import os`;
       `json` and `logger` are already in the module.
     - Catch `OSError` and `json.JSONDecodeError` (`ValueError`), as
       `agents/coder.py:1160-1175` does.
     - Leave the CLI-mode `sys.exit(1)` at 380 unchanged.
     - The other `sys.exit` sites (190, 288, 309, 377, 380, 543, 705) need no
       edit; they now run C1's `finally`. Site 190 is the #1673
       held-migration exit and is covered too.
   - **`apps/backend/core/workspace_fetch.py:208-236` (C3), `maybe_fetch_usage`.**
     - Remove the `dest.is_file()` return at 218-219 and always GET
       `_usage_key(spec_id)`. Keep the broad `except` at 224-226: it covers
       404s and the `RuntimeError` raised when there is no `S3_ENDPOINT`.
     - Skip the object when `len(data) > 1 << 20`, `json.loads` fails, or the
       result is not a dict.
     - Read the local `totalTokens`. A missing or unreadable local file means
       "write any valid dict".
     - Otherwise write only when the fetched `totalTokens` is strictly
       higher. Write via `tempfile.mkstemp(dir=dest.parent)` plus
       `Path(tmp).replace(dest)`; if the replace fails, remove the temp file
       with `Path(tmp).unlink()` and return False.
     - Return True only on a write. Update the docstring.
   - Verify by:
     - `$PY -m pytest tests/test_workspace_fetch.py tests/test_cli_build_push_on_exit.py -q -p no:cacheprovider`
       (all pass).
     - `ruff format --check apps/backend && ruff check apps/backend`.
     - Both ratchets (see "Tests" item 5).
   - Traps:
     - **Missing local file.** It must mean "accept any valid dict", not
       "local is 0". The existing `test_usage_round_trip` payload
       `{"total_tokens":123}` has no `totalTokens`, so the stricter reading
       breaks it.
     - **`finally` placement.** A `finally` cannot swallow `SystemExit`, and
       the helpers never raise, so the exit code is preserved. Do not put the
       `finally` inside an `except`.
     - **CodeQL.** Log only a constant label plus the byte size. Never log a
       variable named `*token*` or `*total*`, `spec_id`, or file content.
     - **JSON module.** Use the stdlib `json` module for the
       `build_commands.py` write.
     - **Ratchet: PLR0402.** `import core.workspace_fetch as workspace_fetch`
       adds one PLR0402 to each file that uses it (`main.py`,
       `build_commands.py`, `agent_kubejob.py`), and each base count is 0.
       Use `from core import workspace_fetch`.
     - **Ratchet: PTH105/PTH108.** `workspace_fetch.py` has 0 strict-ruff
       findings, so `os.replace` or `os.unlink` regresses it. Use
       `Path.replace` and `Path.unlink`. The test that patches
       `wf.os.replace` still works, because `pathlib` calls `os.replace`
       (verified on Python 3.12).
     - **mypy --strict.** It runs over whole changed files, so type new code
       (`dict[str, object]`) and add no `Any`.

3. **`apps/web-server/server/services/completion.py`: C6 and C7.**
   - **New helpers, before line 389.** Add `class _BadUsage(Exception)` and
     `def _num(v: object, cap: float) -> float`. `_num` returns 0 for None;
     raises `_BadUsage` for bool, non-int/float, NaN, ±inf, `v < 0` and
     `v > cap`; and returns `v` otherwise. Add a small `_str128` helper too.
     Add `import math` for `math.isfinite`.
   - **`_worker_records` (210-252).**
     - Replace the `int()`/`float()` plus `or 0` conversions at 225-226 and
       235-237 with `_num`. Caps: tokens 1e12, cost 1e6, `duration_ms` 1e10.
     - Integer fields stay int via `int(_num(...))`.
     - Strings become `str[:128]`, or None when not a str.
     - Keep the sort, then take `[:256]`, and let `_BadUsage` propagate.
   - **`usage_from_aggregate` (389-450).**
     - Use `_num` for the fields at 404-420, and cut `model` to 128
       characters.
     - Keep `cost_usd` as `round(float(_num(...)), 6)`. `_num` returns the
       int `0` for None, and `round(0, 6)` is the int `0`, which would turn
       today's `"cost_usd": 0.0` into `0`.
     - Annotate `block: dict[str, Any] = {...}`. With `model` going through
       `_str128`, mypy infers `dict[str, float | int | str | None]`, and the
       later `workers`/`by_*`/`budget` assignments add 4 strict errors,
       which fails the mypy ratchet.
     - Wrap the mapping (scalars, cache fields and `_worker_records`) in
       `except _BadUsage: return None`.
     - C7: delete `_emit_worker_metrics(workers)` and its comment at 429-432.
   - **`emit_terminal_completion` (960-1037), C7.** After
     `usage = read_usage(spec_dir)` at 1003, call
     `_emit_worker_metrics((usage or {}).get("workers") or [])` once. Leave
     `_emit_worker_metrics` (453-465), `read_usage` (367-386) and
     `emit_usage_snapshot` (1040-1084) unchanged.
   - Verify by:
     - `$PY -m pytest tests/test_usage_snapshot.py tests/test_terminal_completion_characterization.py -q -p no:cacheprovider`
       (all pass).
     - ruff, then both ratchets over `completion.py`.
   - Traps:
     - **Byte-identical output.** A valid block must not change: do not
       reorder keys, and do not turn an int into a float (golden test). The
       `GOLDEN` string below was re-checked against `ef25678b`, and it
       matches.
     - **CodeQL.** Never log a rejected value; the names carry "token".
     - **mypy.** The whole file is checked; the number of bare `dict`
       annotations must not grow.

4. **Control plane: C2, C4, C5.**
   - **New method, `apps/web-server/server/services/agent_kubejob.py` (C2).**
     Add `async def _report_kubejob_usage(self, job_id: str, status: str) -> None`
     after `_record_kubejob_terminal`, before `_on_kubejob_build_done` at 197.
     - Partition `job_id`, and return if `spec_id` is empty.
     - Call `resolve_project_path` and `spec_dir_for`.
     - Lazy-import `from core import workspace_fetch  # noqa: PLC0415` and
       `from . import completion  # noqa: PLC0415` (see the 572-575 pattern).
     - Run `fetched = await asyncio.to_thread(workspace_fetch.maybe_fetch_usage, spec_dir, spec_id)`,
       following the 750 pattern.
     - Only if `fetched`, call
       `completion.emit_usage_snapshot(spec_dir, task_id=job_id, project_id=project_id, spec_id=spec_id, status=status)`.
     - Wrap everything in `except Exception`, and log at debug with
       `sanitize_log(job_id)` only.
   - **Three call sites in `agent_kubejob.py`, each with status `"failed"` (C2).**
     - `_on_kubejob_build_failed` (133-166): after the
       `_record_kubejob_terminal` try at 158-165, before `_drain_queue` at
       166. This covers reconcile, `build_backend._fail` (1611+), the
       timeout and `reap_vanished_jobs` (1498+).
     - `reap_abandoned_tasks` (821-893): inside the try at 878-892, after
       `_update_plan_status(..., "failed", ...)` at 879-881.
     - `_stop_kubejob_build` (1006-1052): after the `mark_terminal` try
       (1038-1046), the credential release (1049) and the status emit
       (1050), before `_drain_queue` at 1051. Decision 4: do not touch
       `task_control`. The
       `_safe_emit_task_status(task_id, "human_review", "errors")` call at
       1050 stays.
   - **`_on_kubejob_build_done` (190-224), C4.**
     - After `_release_task_credential` at 203, run
       `await asyncio.to_thread(workspace_fetch.maybe_fetch_plan, spec_dir, spec_id)`,
       then read `implementation_plan.json` best-effort, also in a thread.
     - Do the fetch and the read in a **module-level** async function
       (for example `_kubejob_review_reason(job_id) -> str | None`), not a new
       `self.` method. `tests/test_failed_build_reaches_the_task.py:108-128`
       calls `KubejobMixin._on_kubejob_build_done` with a duck-typed
       `_Recorder` host. A new method called on the non-pause path fails 3 of
       its tests with `AttributeError` (verified). `resolve_project_path`
       must be inside the function's `try`, because that test passes job id
       `p:160-spec` for a project that does not exist. Methods called only on
       the pause path, such as `_report_kubejob_usage`, are fine.
     - When `status == "human_review"` and `reviewReason` is in
       `("plan_review", "injection_scan")`:
       - call
         `task_control.write_control(spec_dir, status="human_review", review_reason=reason, updated_by="kubejob_review_pause")`
         (add `task_control` to the existing
         `from server.services import review_redrive_service` at line 23, and
         add `import json`);
       - then `await self._report_kubejob_usage(job_id, "human_review")`;
       - skip the emit and record try-blocks (208-222);
       - still run `_report_orphaned_worktrees` (223) and `_drain_queue`
         (224).
     - Every other status keeps today's path unchanged.
   - **`apps/web-server/server/services/agent_service.py:366-406` (C5).** Move
     the running-cost comment and the
     `if lifecycle == "review" and spec_dir is not None:` block (383-406)
     up to just above the #1628 comment (366-373). The #1628 comment stays
     directly above `if await self._k8s_job_owns(task_id): return` (374-375),
     which it explains. The `mark_terminal` try (376-382) stays below that
     return. This is a pure move.
   - Verify by:
     - `$PY -m pytest tests/test_agent_kubejob_mixin.py tests/test_agent_service_kubejob_backend.py tests/test_exit_does_not_bury_a_kubejob.py -q -p no:cacheprovider`
       (all pass).
     - ruff, then both ratchets over `agent_kubejob.py` and
       `agent_service.py`.
   - Traps:
     - **One event per failure.** The "new data" gate is the only thing that
       prevents a duplicate `failed` event, so never emit without `fetched`.
       The helper must run after `_record_kubejob_terminal`, whose emit
       fetches usage first.
     - **Blocking I/O.** Do not run `maybe_fetch_plan`, `maybe_fetch_usage`
       or the plan read on the event loop.
     - **CodeQL.** Log only `sanitize_log(job_id)` and exception types.
     - **Mixin typing.** If mypy flags `task_control` or
       `resolve_project_path` on the mixin, declare them in the
       `TYPE_CHECKING` stub block (73-97). Reuse the existing pattern
       such as `_update_plan_status`, and add no new `Any`. In the scratch
       run, neither was flagged.
     - **Mutation checks.** The coder cannot stash: break the code by
       editing, run the test, then edit it back.

5. **`CHANGELOG.md`, follow-ups, PR (session, not the coder).**
   - Add a `CHANGELOG.md` entry under `[Unreleased]` / Fixed (#1633).
   - Record any deviation in this plan in the same commit as the code.
   - Draft the follow-up issues; drafts only, never filed by an agent:
     - TFactory: usage is reported only at a terminal outcome (D1).
     - CFactory: "no model called" vs "nothing reported yet" (D1).
     - AIFactory: a resumed build does not re-send after review (D3).
     - The OTel retry re-emit and the `routes/tasks.py:474` path (C7), if
       wanted.
   - Open the PR to `dev`. The body links intent, spec and plan, names the
     steps the coder did, and lists the C7 caveats and Q-A (unreachable
     today).
   - Verify by the full "Tests" list.
   - Traps:
     - **Commit scope.** It must not contain `#`. Use for example
       `fix(usage): ... (#1633)`.
     - **Environmental failure.** `tests/test_security.py`
       `GitCommitValidator` fails while files are staged; do not count it.
     - **CHANGELOG conflicts.** Expect them against #1670, #1674 and #1677;
       keep both entries.

## Tests

Baseline at `f694bb37` for the six existing files is **32 passed**:
usage_snapshot 2, workspace_fetch 12, kubejob_mixin 2, kubejob_backend 9,
exit_does_not_bury 3, characterization 4. The target is **90 passed** (+58).

### `tests/test_usage_snapshot.py`: C6 (2 → 22)

Helpers: `_ok(**kw)` returns `{"totalInputTokens": 10, "outputTokens": 5, **kw}`,
and a `notify_completion` recorder returns True.

- **`test_usage_rejects_bad_values[case]`** (12 cases). Each asserts
  `usage_from_aggregate(agg) is None`. The cases:
  - `nan_cost`, `nan_tokens`, `inf_tokens`, `neg_inf_cost`
  - `negative` (`totalInputTokens=-5, outputTokens=10`)
  - `abc` (`outputTokens="abc"`)
  - `bool` (`totalInputTokens=True, outputTokens=0`)
  - `over_cap_tokens` (`10**12+1`), `over_cap_cost` (`1e6+1`)
  - `zero` (both 0)
  - `worker_nan`, `worker_duration_over_cap` (`10**10+1`)
- **`test_usage_cap_boundary_accepted`.** Tokens `10**12`, cost `1e6` and
  worker `duration_ms` `10**10` give a block. The token fields and the
  worker's `duration_ms` are ints.
- **`test_usage_none_counts_as_zero`.** `outputTokens=None` gives 0. A
  missing `totalCostUsd` gives `0.0`, and the assertion is
  `isinstance(u["cost_usd"], float)`, because `0 == 0.0` passes when the
  value is an int.
- **`test_usage_model_truncated_to_128`.**
- **`test_worker_strings_truncated_and_non_str_dropped`.** 300-character
  `worker_id`, `provider`, `model` and `routing_tier` come back with length
  128. `phase=5` gives `phase is None`.
- **`test_workers_limited_to_256`.** 300 workers keyed `w000`..`w299` give
  256 workers, the first `w000` and the last `w255`, and
  `by_model[m]["workers"] == 256`.
- **`test_valid_block_is_byte_identical`.** Patch `_emit_worker_metrics` to a
  no-op. `json.dumps(usage_from_aggregate(AGG), sort_keys=True) == GOLDEN`.
  Both values were captured from today's code.
  - Input: `AGG = {"totalInputTokens":1000,"outputTokens":250,"totalTokens":1250,"totalCostUsd":0.0123456789,"model":"claude-sonnet-4-6","cacheReadTokens":40,"cacheCreationTokens":2,"workers":{"w2":{"worker_id":"w2","phase":"coding","subtask_id":"1.1","provider":"anthropic","model":"claude-sonnet-4-6","input_tokens":600,"output_tokens":100,"cost_usd":0.006,"duration_ms":1500,"routing_tier":"standard"},"w1":{"phase":"planning","provider":"anthropic","model":"claude-sonnet-4-6","input_tokens":400,"output_tokens":150,"total_tokens":550,"cost_usd":0.0063456789,"duration_ms":900}}}`
  - Expected: `GOLDEN = '{"by_model": {"claude-sonnet-4-6": {"billing_mode": "unknown", "cost_usd": 0.012346, "duration_ms": 2400, "input_tokens": 1000, "output_tokens": 250, "total_tokens": 1250, "workers": 2}}, "by_provider": {"anthropic": {"billing_mode": "unknown", "cost_usd": 0.012346, "duration_ms": 2400, "input_tokens": 1000, "output_tokens": 250, "total_tokens": 1250, "workers": 2}}, "cache_creation_tokens": 2, "cache_read_tokens": 40, "cost_usd": 0.012346, "input_tokens": 1000, "model": "claude-sonnet-4-6", "output_tokens": 250, "total_tokens": 1250, "workers": [{"billing_mode": "unknown", "cost_usd": 0.006346, "duration_ms": 900, "input_tokens": 400, "model": "claude-sonnet-4-6", "output_tokens": 150, "phase": "planning", "provider": "anthropic", "subtask_id": null, "total_tokens": 550, "worker_id": "w1"}, {"billing_mode": "unknown", "cost_usd": 0.006, "duration_ms": 1500, "input_tokens": 600, "model": "claude-sonnet-4-6", "output_tokens": 100, "phase": "coding", "provider": "anthropic", "routing_tier": "standard", "subtask_id": "1.1", "total_tokens": 700, "worker_id": "w2"}]}'`
- **`test_bad_usage_file_still_sends_terminal_event_and_marker`.**
  - Setup: `delenv S3_ENDPOINT`, and write `token_usage.json` as the raw text
    `{"totalInputTokens": NaN, "outputTokens": 5}`.
  - Call `completion_orchestration.run_terminal_completion(spec_dir=…, project_path=tmp_path, spec_id="s", task_id="p:s", backend_path=None, is_terminal=True, is_completed=False, terminal_status="failed", logger=logging.getLogger("t"))`.
  - Assert one `failed` event, `"usage" not in ev`, and that
    `.terminal_completion_emitted` exists.
- **`test_bad_usage_file_snapshot_is_noop`.** With the same file,
  `emit_usage_snapshot(...)` returns None and sends 0 events.

### `tests/test_workspace_fetch.py`: C3 (12 → 22)

Helper `_seed(monkeypatch, payload)` installs `a_s._fake_store()` as
`ArtifactStore` and puts the payload at `wf._usage_key("042-x")` when it is
not None.

- **Rewrite `test_fetch_usage_noop_when_present`** as
  `test_fetch_usage_without_s3_endpoint_keeps_local`. With no `S3_ENDPOINT`
  and local `{"totalTokens":5}`, it returns False and the bytes are
  unchanged.
- **Keep `test_usage_round_trip` unchanged.** This is the missing-local trap.
- **`test_fetch_usage_overwrites_lower_local`.** Local 100, remote 500:
  returns True and the local file now reads 500.
- **`test_fetch_usage_keeps_equal_or_higher_local[500|900]`.** Remote 500:
  returns False and the bytes are identical.
- **`test_fetch_usage_unreadable_local_is_lower`.** Local `b"not json"`,
  remote 500: returns True.
- **`test_fetch_usage_size_cap[exact|over]`.** The payload
  `{"totalTokens":10**6}` is padded with spaces to `1<<20` bytes (True) or
  `(1<<20)+1` bytes (False, local unchanged).
- **`test_fetch_usage_skips_bad_payload[non_json|non_dict]`.** Payloads
  `b"\x00garbage{"` and `b"[1, 2]"`: returns False, local unchanged.
- **`test_fetch_usage_no_object_leaves_local`.** Returns False, local
  unchanged.
- **`test_fetch_usage_failed_replace_leaves_local`.** Patch `wf.os.replace`
  to raise `OSError`. Returns False, the local file still reads 100, and
  `token_usage.json` is the only file left in the directory.

### `tests/test_agent_kubejob_mixin.py` (still 2)

Add `"_report_kubejob_usage"` to `_KUBEJOB_METHODS`.

### `tests/test_agent_service_kubejob_backend.py`: C2 and C4 (9 → 22)

A shared builder `_kj(tmp_path, monkeypatch, *, plan=None, remote_plan=None, usage=True)`
needs no DB. It follows `test_terminal_completion_characterization._make_service`:

- **Store.** It uses `a_s._fake_store()`. Usage
  `{"totalInputTokens":400,"outputTokens":100,"totalTokens":500,"totalCostUsd":0.01,"model":"m"}`
  goes at `_usage_key("042-x")`, and the optional remote plan at
  `_plan_key`.
- **Paths.** `resolve_project_path` returns `tmp_path`.
- **Recorders.** It records `_report_orphaned_worktrees` (orphans),
  `completion.notify_completion` (events) and `_drain_queue` (drained).
- **Stubs.** `_release_task_credential`, `_safe_emit_task_status` and
  `_safe_emit_task_update` are stubbed.

`PHASED = {"phases":[{"name":"b","subtasks":[{"id":"1","status":"completed"}]}],"status":"in_progress"}`.

C2 cases:

- **`test_kubejob_failed_without_phases_sends_one_failed_snapshot`.** Events
  are `["failed"]` with `usage.total_tokens == 500`, and `drained == [1]`.
- **`test_kubejob_failed_with_phases_sends_one_failed_event_total`.** Exactly
  1 event, `failed`, carrying usage. This pins the helper running after
  `_record_kubejob_terminal`.
- **`test_kubejob_stop_reports_usage_and_leaves_control[object|no_object]`.**
  - Setup: control is `in_progress`. The store state is
    `running`/`k8s-job`, the backend has an async `delete_job`, and the log
    stream and console are stubbed.
  - Assert the call returns True, events are `["failed"]` or `[]`, the
    control status and `updatedBy` are unchanged, `mark_terminal` was called
    once, and `drained == [1]`.
- **`test_kubejob_reap_reports_one_snapshot`.**
  - Setup: patch `load_projects`, `get_spec_dirs` and `spec_to_task` (an old
    `in_progress` task), with `is_running` False and liveness `"absent"`.
  - Assert reaped `== [TASK]` and events are `["failed"]`.
- **`test_kubejob_usage_helper_raising_never_blocks[failed|stop]`.** Patch
  `wf.maybe_fetch_usage` to raise. Nothing raises, `mark_terminal` was called
  (stop path), and `drained == [1]`.

C4 cases. Patch `completion_orchestration.run_terminal_completion` with
`rtc = AsyncMock(return_value="completed")`.

- **`test_kubejob_review_pause_is_not_completion[plan_review|injection_scan]`.**
  - Setup: remote plan `{**PHASED, "status": "human_review", "reviewReason": R}`.
  - Control: `human_review`, with reason R and `updatedBy == "kubejob_review_pause"`.
  - Events: `["human_review"]`, `rtc.await_count == 0`, and no
    `.terminal_completion_emitted`.
  - Plan and bookkeeping: the plan status is still `human_review`,
    `orphans == [TASK]`, `drained == [1]`.
- **`test_kubejob_preflight_skeleton_plan_pauses`.** Remote plan
  `{"phases":[],"status":"human_review","reviewReason":"plan_review"}` and no
  local plan. Same assertions as above.
- **`test_kubejob_other_statuses_keep_handoff[hr_completed|hr_qa_issues|in_progress]`.**
  - `rtc.await_count >= 1`, and some call has `is_completed=True`.
  - `updatedBy != "kubejob_review_pause"`, `events == []`, `drained == [1]`.

### `tests/test_exit_does_not_bury_a_kubejob.py`: C5 (3 → 5)

Both tests set control to `human_review` and record
`completion.emit_usage_snapshot`.

- **`test_k8s_job_owned_review_task_still_reports_usage`.** One snapshot,
  status `human_review`, and `terminal_calls == []`.
- **`test_subprocess_review_task_reports_usage_and_goes_terminal`.** One
  snapshot, and `terminal_calls == [(TASK, "review")]`.

### `tests/test_terminal_completion_characterization.py`: C7 (4 → 6)

Usage with workers `w1` and `w2` (not `main`). Record
`completion._emit_worker_metrics`.

- **`test_worker_metrics_emitted_once_per_terminal_event`.** One call, with
  2 workers.
- **`test_worker_metrics_never_from_snapshot_or_live_mapping`.** A snapshot
  sends 1 event and makes 0 calls. `usage_from_aggregate` makes 0 calls.

### New `tests/test_cli_build_push_on_exit.py`: C1 and Decision 5 (11 tests)

Insert `apps/backend` into `sys.path`. Patch the six `wf.maybe_push_*`
helpers with recorders, `wf.maybe_unpack_workspace` to return False,
`cli_main.setup_environment` to return `tmp_path` (so `.env` cannot inject
`WORKSPACE_URI`), `cli_main.find_spec` and `cli_main.handle_build_command`.
`delenv` `TRACEPARENT` and `WORKSPACE_URI`. Set `sys.argv` to
`run.py --spec 001-x --project-dir <tmp> --auto-continue [--stop-after-planning]`.

- **`test_exit_pushes_usage_and_keeps_code[1-build|0-build|1-plan_only|0-plan_only]`.**
  - The exit code is preserved.
  - Usage is pushed once, with `(spec_dir, "001-x")`.
  - The plan is pushed once for a build, and 0 times for plan-only.
  - Branch, memory, gate-marker and task_logs are each pushed 0 times.
- **`test_success_pushes_each_once[build|plan_only]`.** For build, all six
  are pushed exactly once. For plan-only, usage is pushed once and everything
  else 0 times.
- **`test_crash_still_pushes_usage`.** A `RuntimeError` propagates, usage is
  pushed once, and the branch 0 times.
- **`test_preflight_pause_plan[uri_missing_plan|uri_existing_plan|no_uri|cli_mode]`.**
  - Setup: drive `build_commands.handle_build_command` with the patches from
    `tests/test_solo_mode.py:368-412`, plus `AIFACTORY_AUTH_PREFLIGHT=off`.
    Mock `ReviewState` so that `is_approval_valid` is False and `approved` is
    False. Use `force_bypass_approval=False`.
  - Exit code: 0, or 1 for `cli_mode`.
  - `uri_missing_plan`: the skeleton is written with
    `human_review`/`plan_review`.
  - `uri_existing_plan`: the phases are kept, and status and reason are set.
  - `no_uri` and `cli_mode`: no plan file is written.
  - `cli_mode` sets `WORKSPACE_URI` and passes `auto_continue=False`.
    Without the URI, the "plan write moved above `if auto_continue`"
    mutation survives.

### Mutation checks

Apply each by editing, run the named test, then edit back. Each must turn the
test red.

| Mutation | Test that fails |
|---|---|
| drop the NaN/finite check in `_num` | `rejects_bad_values[nan_cost]` |
| drop the bool check | `[bool]` |
| drop `v < 0` | `[negative]` |
| drop the cap compare | `[over_cap_tokens]` |
| cap `>` becomes `>=` | `cap_boundary_accepted` |
| `round(_num(...), 6)` without `float()` for cost | `usage_none_counts_as_zero` |
| token fields returned as float | `valid_block_is_byte_identical` |
| remove `except _BadUsage: return None` | `bad_usage_file_still_sends_terminal_event_and_marker` |
| drop `[:256]` / `[:128]` | `workers_limited_to_256` / `model_truncated_to_128` |
| C3 strictly-higher compare loosened to higher-or-equal | `keeps_equal_or_higher[500]`, `failed_with_phases…` |
| missing local treated as 0 | `test_usage_round_trip` |
| drop the size cap | `size_cap[over]` |
| `write_bytes` instead of temp file + `os.replace` | `failed_replace_leaves_local` |
| delete the helper call in `_on_kubejob_build_failed` | `failed_without_phases…` |
| helper called before `_record_kubejob_terminal` | `failed_with_phases…` |
| delete the helper call in stop / reap | `stop…[object]` / `reap_reports_one_snapshot` |
| drop the helper's `try/except` | `helper_raising_never_blocks[failed]` |
| plan read made a `self.` method | `test_failed_build_reaches_the_task.py` (3 existing tests) |
| stop snapshot status `human_review`, or `write_control` on stop | `stop…` |
| pause allowlist widened to any `human_review` | `other_statuses_keep_handoff[hr_completed]` |
| keep `_record_kubejob_terminal` on pause | `review_pause_is_not_completion` |
| `return` before orphans/drain on pause | `review_pause…` |
| C5 block moved back below `_k8s_job_owns` | `k8s_job_owned_review_task_still_reports_usage` |
| `_emit_worker_metrics` back in `usage_from_aggregate` | both C7 tests |
| usage push back in the success block | `exit_pushes_usage…[*]` |
| drop the `not stop_after_planning` guard in `finally` | `exit_pushes…[*-plan_only]` |
| old usage call left in the success block | `success_pushes_each_once[build]` |
| plan write moved above `if auto_continue` | `preflight_pause_plan[cli_mode]` |
| `WORKSPACE_URI` check dropped | `preflight_pause_plan[no_uri]` |

### Commands and expected results

Run from the worktree with
`PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python`.

1. **Focused tests**: expect **90 passed**. Also run
   `tests/test_failed_build_reaches_the_task.py` and
   `tests/test_memory_packed_path.py`, which exercise the same two code paths
   (expect 24 passed, unchanged).
   `$PY -m pytest tests/test_usage_snapshot.py tests/test_workspace_fetch.py tests/test_agent_kubejob_mixin.py tests/test_agent_service_kubejob_backend.py tests/test_exit_does_not_bury_a_kubejob.py tests/test_terminal_completion_characterization.py tests/test_cli_build_push_on_exit.py -q -p no:cacheprovider`
2. **Collection check**: expect **11** collected.
   `$PY -m pytest --collect-only -q tests/test_cli_build_push_on_exit.py`
3. **Full suites**: record each baseline before Step 1, then expect the
   baseline +58 and 0 new failures. The `GitCommitValidator` failure in
   `tests/test_security.py` while files are staged is environmental.
   - `$PY -m pytest tests/ -m "not slow" -q`
   - `$PY -m pytest apps/backend -q -o asyncio_mode=auto`
   - `$PY -m pytest apps/web-server/tests -q -o asyncio_mode=auto`
4. **Ruff**: both commands clean.
   - `ruff format --check apps/backend apps/web-server scripts tests`
   - `ruff check apps/backend apps/web-server scripts tests`
5. **Ratchets**, after `git add`: both pass over `main.py`,
   `build_commands.py`, `workspace_fetch.py`, `agent_kubejob.py`,
   `agent_service.py` and `completion.py`.
   - `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   - The same command with
     `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`.
6. **CodeQL hygiene**: log calls carry only `sanitize_log(job_id)`, the spec
   id, byte sizes and exception type names.
7. **Security lint**: `$PY security-lint/security_lint.py .` is clean.
8. **Autonomy matrix**: `$PY scripts/gen_autonomy_matrix.py --check` shows
   no diff.
9. **Spawns**: no subprocess spawns are added, so
   `tests/test_no_unscrubbed_spawn.py` is unaffected.

## Rollback

`git revert <merge-sha>`. There are no flags, schema or env changes, so
nothing needs migrating.

- **Leftover state stays valid.** `token_usage.json` overwrites only ever
  raise the total. Control entries with `updatedBy: kubejob_review_pause`
  are correct `human_review` states, and paused tasks resume through normal
  approval.
- **What a revert brings back.** No usage on failed, stopped or reaped
  kubejob builds; review pauses reported as `completed`; and a bad
  `token_usage.json` blocking the terminal event.
- **Partial revert.** Revert just the step's commit. C6/C7 (`completion.py`)
  and C3 (`workspace_fetch.py`) are self-contained. C2 and C4 depend on C3:
  without the strictly-higher rule, a failed build with phases sends 2
  events.

## Deviations

- Step 1: of the 58 new cases, 36 fail before the code change and 22 already
  pass (they pin invariants, or only go red under a mutation once the code
  exists). `worker_metrics_emitted_once_per_terminal_event` passes before and
  after C7, so `never_from_snapshot_or_live_mapping` is the C7 guard. The four
  `preflight_pause_plan` cases are separate functions sharing a helper.
  `test_agent_service_kubejob_backend.py` gains a `sys.path` insert for
  `apps/backend`.
