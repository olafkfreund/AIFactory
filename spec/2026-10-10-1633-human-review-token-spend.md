---
status: approved
issue: 1633
intent: intent/2026-10-10-1633-human-review-token-spend.md
---

# Spec: kubejob builds must report the tokens they spent

## Design

We take the minimal design (reuse `emit_usage_snapshot`, `maybe_fetch_usage`
and `maybe_push_usage`; five files) and add four parts of the robust design.
Each of those four closes a gap that the code shows the minimal design would
open. The four are marked **[B]** below. The base is `d56073c0`.

### Proposed answers to the intent's open questions

These are proposed defaults for you to confirm or change.

| # | Question | Proposed answer | Why |
| - | -------- | --------------- | --- |
| 0 | Is #1633 already fixed on dev? | No. Write the spec. | Dev is one commit past the intent's base (#1672, merge tier), and that commit does not touch usage. All the gaps are still in the code. A failed build with no plan gets a minimal plan with `phases: []` (`agent_service.py:470-481`), then returns `invalid_plan` (`:518-526`) before `run_terminal_completion` (`:602`). Stop (`agent_kubejob.py:1019-1031`) only marks the task terminal and sends a status update. In the Job, `build_commands.py:264/353/520` call `sys.exit` before `maybe_push_usage` (`cli/main.py:552`). |
| 1 | Scope | Fix the AIFactory kubejob gaps only. Open two follow-up issues: TFactory (usage is reported only at a terminal outcome) and CFactory ("no model called" vs "nothing reported yet"). Neither blocks this PR. | The intent rules out depending on CFactory or TFactory. The event envelope does not change. |
| 2 | Stopped, reaped and abandoned builds | Yes, all of them, through one shared helper (C2). Send usage only when a pushed `token_usage.json` can be read and holds new spend. Otherwise send nothing, and never a zero. | The object store can be read from any replica (`workspace_fetch.py:207-235`). The snapshot is cumulative. `usage_from_aggregate` already returns `None` when tokens are zero (`completion.py:402-407`). |
| 3 | Resend after a resumed review | No. Move it to a follow-up issue. The `.terminal_completion_emitted` marker is unchanged, and the kubejob review pause (C3) does not write it. | The marker is the dedupe guard (`completion_orchestration.py:141-170`). Splitting it changes behaviour and could cause duplicate events. |
| 4 | Gather MyFriends logs first? | No. Go ahead on the code findings. Logs are optional confirmation. | A unit test can show each gap. |
| 5 | #1669, #1670, #1677 | Write the spec and plan now. Start coding after #1670 merges, then rebase on dev. Do not wait for #1677. | #1670 (PR #1694, still open) is the only change that edits the same kubejob functions (`agent_kubejob.py`, 37 lines). #1669 has already merged to dev (`2cf18708`): it adds `is_running_anywhere` after `reap_abandoned_tasks` and edits none of the functions below. Line numbers here are against `d56073c0`; after the rebase, `agent_kubejob.py` lines past `:90` move by +2 and past `:903` by +30 (the stop path becomes `:1049-1061`). |

### C1. In the Job, push usage on every exit (`apps/backend/cli/main.py:505-558`)

- Wrap `handle_build_command(...)` (`:506`) in `try/finally`.
- The `finally` calls `maybe_push_usage(spec_dir, spec_dir.name)` every time.
  It also calls `maybe_push_plan(...)` when `not args.stop_after_planning`,
  because C4 reads the paused plan from the object store.
- Remove those two calls from the existing success block so nothing is pushed
  twice.
- The branch, memory, gate-marker and task_logs pushes stay where they are.
  This means a failed branch is never pushed.
- Python runs `finally` on `SystemExit`, so the exits at `build_commands.py`
  `:264,285,353,356,520` push usage too. Both push helpers never raise
  (`workspace_fetch.py:175-205,340`), so the exit code is preserved.
- Planning-only Jobs (`agent_kubejob.py:276,355`) spend tokens and push no
  usage today. They now push it.
- Out of reach, by decision: a crashed, OOM-killed or SIGKILLed Job. The #1249
  live snapshots (`agent_emit.py:436-490`) cover those builds.

### C2. One shared kubejob usage helper (`apps/web-server/server/services/agent_kubejob.py`)

Add `AgentService._report_kubejob_usage(job_id: str, status: str) -> None` next
to `_record_kubejob_terminal` (`:173`). It:

- splits `job_id`, then calls `resolve_project_path` and `spec_dir_for`;
- runs `maybe_fetch_usage` in `asyncio.to_thread`, because `get_bytes` is a
  blocking boto call; the same pattern is at `:758`;
- **[B]** calls `emit_usage_snapshot(spec_dir, task_id=job_id, project_id=…,
  spec_id=…, status=status)` (`completion.py:1040-1080`) **only when the fetch
  returned `True`**, meaning new spend arrived;
- catches every exception and logs it at debug level with
  `sanitize_log(job_id)` only.

The "new data" gate is the only way to avoid a duplicate `failed` event without
reading the #1407 marker. When the plan has phases, `_update_plan_status` →
`run_terminal_completion` has already fetched usage and sent it in the terminal
`failed` event. The helper's fetch then returns `False`, and nothing is sent
again. CFactory dedupes by event id, not by status (`CFactory store.py:197-205`),
so an ungated second `failed` event would be counted twice.

The helper is called after the existing bookkeeping in three places:

- `_on_kubejob_build_failed` (`:138-171`), after `_record_kubejob_terminal(job_id,
  "failed")`, with status `"failed"`. This covers reconcile, `_fail`, the
  timeout and the vanished-Job reaper (`build_backend.py:1591-1609`).
- `_stop_kubejob_build` (`:1019-1031`), after `mark_terminal` and before
  `_drain_queue`, with status `"failed"`. Note that this path already sends
  task status `human_review/errors` (`:1030`), so the cockpit and the board
  show different labels for a stopped build. See Decision 4.
- `reap_abandoned_tasks` (`:886-888`), after `_update_plan_status(..., "failed")`
  succeeds, with status `"failed"`.

### C3. Refresh the usage fetch, never with a lower total [B] (`apps/backend/core/workspace_fetch.py:207-235`)

Today `maybe_fetch_usage` returns early when a local file exists (`:217-219`).
After any earlier fetch, a rerun's or resumed Job's newer totals are never
read, and the new paths would send the old figure. CFactory replaces the stored
usage with the latest one it receives (`CFactory store.py:281-288`). A stale,
lower snapshot would overwrite the higher #1249 live figure, which breaks
"never overwrite real spend".

The change:

- Remove the `dest.is_file()` early return.
- Fetch the object. Skip it if it is over 1 MiB, is not valid JSON, or is not a
  dict. Log only the size.
- Write it (temp file then `os.replace`) only when the local copy is missing,
  unreadable, or has a strictly lower `totalTokens`. Return `True` only when a
  write happened.
- On the co-mount path no object exists. The local file is kept, and the only
  cost is one 404 GET.
- On an install with no object store (the default subprocess install, no
  `S3_ENDPOINT`), `ArtifactStore()` raises `RuntimeError`
  (`core/artifact_store.py:181-186`). The existing `except` catches it, the
  local file is kept and the call returns `False`, as it does today. This
  function's only caller today is `completion.py:984`.

This also fixes the existing stale-local bug on the done path.

### C4. Report the review pause on kubejob [B guard] (`agent_kubejob.py:195-229`)

In `_on_kubejob_build_done`, before `_emit_kubejob_terminal_completion`:

- Run `maybe_fetch_plan(spec_dir, spec_id)` in a thread. It overwrites the local
  copy (`workspace_fetch.py:378-407`). `run_terminal_completion` fetches the
  plan again, which is harmless.
- Read `implementation_plan.json`. Treat the build as paused **only if**
  `status == "human_review"` **and** `reviewReason in {"plan_review",
  "injection_scan"}` (the real pauses: `agents/coder.py:573`, `:1171`).
  - **[B]** A check on `status` alone is wrong. QA approval writes
    `human_review` with `reviewReason="completed"`, and QA rejection writes
    `"qa_issues"` (`agents/tools_pkg/tools/qa.py:494-500`). A status-only check
    would take every QA-approved build off its TFactory handoff and PR endgame.
- When the build is paused:
  - call `task_control.write_control(spec_dir, status="human_review",
    review_reason=<reason>, updated_by="kubejob_review_pause")`. **[B]** Without
    this, the control status stays `in_progress`, because control overrides the
    plan (`routes/task_service.py:893-897`). `reap_abandoned_tasks` would then
    fail the paused task.
  - call `await self._report_kubejob_usage(job_id, "human_review")`.
  - **skip** `_emit_kubejob_terminal_completion` and `_record_kubejob_terminal`.
    That means no TFactory handoff, no PR endgame and no #1407 marker. **[B]**
    Sending it through `_update_plan_status("human_review")` would write the
    marker, because PLAN_REVIEW counts as terminal there
    (`agent_service.py:602-624`). The kubejob path would then get the same
    "resumed build never reports" bug that Q3 deferred.
- Credential release, `_report_orphaned_worktrees` and `_drain_queue` still run.
- Every other plan status keeps today's path, including QA's
  `human_review/completed`. `terminal_status="completed"` at `:265` is left
  alone.
- **Gap: the pre-flight approval pause.** When `should_pass_force` is false
  (`build_backend.py:718-719`), a review-gated build stops in the Job at the
  pre-flight check: it saves `review_state.json` and calls `sys.exit(0)`
  (`cli/build_commands.py:348-353`). It never writes `human_review` to the plan,
  so the check above misses it, and the build is reported `completed`, or
  `failed` by the evidence gate. Proposed default: on that branch, and only on
  the packed path (`WORKSPACE_URI` set), write `status="human_review"`,
  `reviewReason="plan_review"` to the plan before the exit. Write a
  `{"phases": []}` skeleton when no plan exists, as `agents/coder.py:1160-1171`
  does. C1 then pushes it. The subprocess path is unchanged. See Decision 5.
- The reason comes from an allowlist. A forged plan can at most skip a handoff,
  which fails safe. It can never trigger one.

### C5. Spec-creation snapshot for Job-owned tasks (`agent_service.py:374-404`)

Move the `if lifecycle == "review" and spec_dir is not None:`
`emit_usage_snapshot` block (`:383-404`) above `if await self._k8s_job_owns(task_id):
return` (`:374-375`). `mark_terminal` stays below the return, so the #1628 guard
still stops the server from finalising a Job-owned task. This is a pure move.

### C6. Treat `token_usage.json` as untrusted (`completion.py:224-235`, `:389-451`)

Every path calls `usage_from_aggregate`, including the live `agent_emit.py:455`,
so the guard goes there and in `_worker_records`.

- Missing or `None` fields still count as 0, as `or 0` does today, so a valid
  file without the optional cache fields is accepted unchanged. Integer fields
  stay `int`.
- Add a private `_num(v: object, cap: float) -> float`. It rejects `bool`,
  values that are not `int`/`float`, NaN, ±inf, negatives and anything over
  the cap, by raising a private `_BadUsage`. `usage_from_aggregate` catches
  `_BadUsage` and returns `None`, so it sends no usage block at all, never a
  partial or zero one.
- Caps: tokens `10**12`, cost `10**6` USD, `duration_ms` `10**10`.
- **[B, changes the default]** A value over a cap is **rejected, not clamped**.
  Clamping would send a number nobody spent.
- String fields (`model`, `provider`, `worker_id`, `phase`, `subtask_id`,
  `routing_tier`) become `v[:128]` when `v` is a `str`, and `None` otherwise.
- `workers` is limited to the first 256 entries in sorted order. This bounds
  the event size and the OTel label cardinality.
- **[B]** This also fixes a bug that exists today. A non-numeric value in the
  file currently makes `int()`/`float()` raise through `emit_terminal_completion`.
  `run_terminal_completion` catches it, writes no marker and never sends the
  terminal event. After this change the event goes out without usage.
- Tenant and project still come from `task_id`, the registry and
  `_read_tenant_id(spec_dir)`. Nothing new is read from the file.

### C7. Emit OTel worker metrics once [B, needs confirmation] (`completion.py:432`)

Move `_emit_worker_metrics(workers)` out of `usage_from_aggregate` and into
`emit_terminal_completion`, which runs under the fire-once marker.

- Today it runs on every snapshot. Its OTel counters (`apps/web-server/server/observability/metrics_otel.py:150`) add
  the cumulative totals again each time: every 10s on the live path, and once
  per new path this spec adds.
- The fix moves one call. Live per-worker metrics stop updating while a build
  runs, and dashboard totals change from inflated to correct.

### Not done

- A SIGTERM → `sys.exit(143)` handler in the packed Job. A Job that is stopped
  or killed at its deadline still pushes nothing, and the #1249 live snapshots
  cover it. If needed, add it in a follow-up issue.
- Any change to the #1407 marker, the event envelope, CFactory or TFactory.

## Alternatives rejected

- **Send failures through `run_terminal_completion(is_completed=False)`.** It
  trips the #1407 marker and the handoff and endgame branches.
  `emit_usage_snapshot` already does this job.
- **Emit usage inside `_update_plan_status` or `_record_kubejob_terminal`.** It
  sits behind the `invalid_plan` early return and has many callers, so it would
  tie status writes to usage.
- **Send a snapshot from the failure hooks without the "new data" gate.** It
  sends a second `failed` event, and CFactory counts both.
- **Detect the pause from `plan.status` alone.** Every QA-approved build would
  lose its handoff (C4).
- **Record the pause through `_update_plan_status("human_review")`.** It writes
  the #1407 marker and replaces `injection_scan` with `plan_review` through
  `phase_to_review_reason`.
- **Clamp out-of-range values.** That sends spend that never happened.
- **Move the whole push block into `finally`.** It would push a failed branch
  to origin.
- **Push from a timer, `atexit` or a signal handler.** That adds new machinery.
  `atexit` does not run on SIGTERM, and the live snapshots already cover a
  killed Job.
- **Delete the usage object at dispatch, or compare `LastModified`.** Both need
  a new `ArtifactStore` API. "Keep the higher total" covers stale objects
  without one.
- **A new usage-only event type or endpoint.** The intent forbids it.
- **Validate in each caller.** Four or more copies would drift apart. One guard
  in `usage_from_aggregate` covers every path.

## Risks

- **"Keep the higher total" assumes a rerun starts from the earlier total.**
  That holds if `copy_spec_to_worktree` carries `token_usage.json` forward, so
  that `record_turn` adds to it (`_read_aggregate`,
  `apps/backend/agents/token_attribution.py:328`). If a rerun starts
  from zero, a genuine lower total is never sent. That fails toward sending
  nothing, and a test pins this behaviour.
- **The stop path races the Job.** The immediate fetch can run before the Job
  pushes. The fallbacks are the live snapshots and, later, the reap path, whose
  fetch picks up a late push. Add a test that stop leaves
  control status alone, so that the reaper does not later relabel the task.
- **Stale plan object.** If a resumed Job exits 0 but its plan push fails, an
  old `plan_review` plan could route the build to a pause. This needs a store
  failure to happen, and it fails safe: there is no handoff.
- **Double emit across replicas.** It is harmless, because the snapshot is
  cumulative and the "new data" gate stops most repeats.
- **C1 changes push order on a clean exit.** Plan and usage are now pushed
  after the branch, memory, gate and task_logs. The control plane reads only
  after the Job exits, so the order does not matter.
- **C7 changes dashboards.** Live per-worker OTel stops updating while a build
  runs.
- **Merge conflict with #1670** in `agent_kubejob.py`. Rebase after it merges.

All of these affect the AIFactory web server and the packed kubejob Job only.
The subprocess backend is affected only by C3, C5, C6 and C7.

Default installs stay unbroken. C1's push helpers are no-ops without
`WORKSPACE_URI` (`workspace_fetch.py:187-188`). C2 and C4 run only on the
kubejob backend. C3 behaves as today without an object store. C6 accepts every
file that is valid today, and the block it builds is byte-identical.

## Verification

Unit tests, in existing files where possible:

- `test_usage_snapshot.py`:
  - `usage_from_aggregate` returns `None` for NaN, ±Infinity, negatives,
    `"abc"`, `True`, over-cap values and zero tokens;
  - a 10k-character `model` is cut to 128 characters;
  - a 300-entry `workers` map is limited to 256 entries;
  - a valid block is byte-identical to today's;
  - a bad file still sends the terminal event, without usage, and writes the
    marker.
- `test_workspace_fetch.py`:
  - a lower local copy is overwritten and the call returns `True`;
  - an equal or higher local copy is kept and the call returns `False`;
  - an object over 1 MiB, or one that is not JSON, is skipped;
  - with no object, the local file is untouched.
- `test_agent_kubejob_mixin.py` / `test_agent_service_kubejob_backend.py`:
  - failure where the plan has no phases → exactly one `failed` snapshot;
  - failure where the plan has phases → exactly one `failed` event in total;
  - stop → one snapshot when an object exists, none when it does not, and
    control status unchanged;
  - reap → one snapshot;
  - the helper raising does not stop `mark_terminal` or `_drain_queue`;
  - pause with `plan_review` or `injection_scan` → control set to
    `human_review`, one `human_review` snapshot, no marker,
    `run_terminal_completion` not called;
  - `human_review/completed` or `qa_issues` → today's path, and the handoff
    runs;
  - a fetched `{"phases": [], "status": "human_review", "reviewReason":
    "plan_review"}` skeleton plan (the pre-flight pause) → paused, no handoff.
- `test_exit_does_not_bury_a_kubejob.py`: a Job-owned task at
  `lifecycle="review"` sends the snapshot, and `mark_terminal` is not called.
- `test_terminal_completion_characterization.py`: OTel worker metrics are
  emitted once per terminal event and never from snapshots.
- CLI test: with `handle_build_command` patched to `sys.exit(1)` and to
  `sys.exit(0)`:
  - `maybe_push_usage` is called and the exit code is preserved;
  - `maybe_push_plan` is called only when not `stop_after_planning`;
  - `maybe_push_workspace_branch` is not called.
- New `tests/test_cli_build_push_on_exit.py` holds the CLI test above, plus: the
  pre-flight pause with `WORKSPACE_URI` set writes the `human_review/plan_review`
  plan before `sys.exit(0)`, and without it writes nothing.
- `test_workspace_fetch.py`: with `S3_ENDPOINT` unset and a local file present,
  `maybe_fetch_usage` returns `False` and leaves the file unchanged.

CI gates:

- `ruff check apps/backend apps/web-server scripts tests` (`ci.yml:84`).
- `pytest tests/ -m "not slow"`, `pytest apps/backend` and
  `pytest apps/web-server/tests` (`ci.yml:190,206,218`), and the
  `test-collection.yml` collection check for the new test file.
- `security-lint/security_lint.py .` (`security-lint.yml:122`).
- `scripts/cq_ratchet.py`: strict ruff plus `mypy --strict` on every changed
  file, tests included, with typed helpers and no new `Any`. Run it locally
  first, because `main.py`, `agent_service.py` and `agent_kubejob.py` are large
  and the ratchet checks the whole file.
- CodeQL: log only `sanitize_log(job_id)`, the spec id, byte sizes and
  exception types. Never log values from the file, `WORKSPACE_URI` or the
  environment.
- `python scripts/gen_autonomy_matrix.py --check`. `merge_policy`,
  `review_tier` and `pr_endgame` are untouched, so there should be no diff.
  Run it anyway, because the workflow has no `paths:` filter.

### Decisions for you

1. **C6 reject vs cap.** Reject over-cap values instead of capping them, as
   the default proposed. Recommended: reject.
2. **C7 OTel move.** Include it, or split it into its own issue. Recommended:
   include it, because it is one moved call.
3. **SIGTERM handler.** Leave it out. Recommended: leave it out, as the default
   accepted.
4. **Snapshot status for a stopped build.** Send `failed`, or `human_review` to
   match the `human_review/errors` task status the stop path already sends.
   Recommended: `failed`, because CFactory has no `errors` reason and the build
   did not finish.
5. **Pre-flight approval pause (C4 gap).** Write the plan status in the Job on
   the packed path, or leave that pause reported as `completed` and open a
   follow-up issue. Recommended: write it, because without it the intent's
   review-state outcome is not met for review-gated builds.
