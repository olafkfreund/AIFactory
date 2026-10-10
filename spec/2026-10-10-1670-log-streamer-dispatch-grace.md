---
status: approved
issue: 1670
intent: intent/2026-10-10-1670-log-streamer-dispatch-grace.md
---

# Spec: the log streamer asks the same "is it alive" question as `is_running()`

## Design

The change deletes code from one source file and edits two tests in one test
file. It adds no helpers. `build_log_stream.py`, `agent_service.py`,
`job_state_store.py` and `CHANGELOG.md` do not change.

Line numbers below are from the current branch head. Deleting lines 17 and
32-37 moves every later line in `agent_kubejob.py` up by 7, so the plan and the
PR should give the new numbers.

### Decisions on the intent's open questions

These are the defaults. Each one can be changed at review.

1. **Q1, a per-row poll error drops the id: accept it here and file a
   follow-up issue.** The `except ... continue` at
   `agent_kubejob.py:698-700` already makes `is_running()` report the build as
   not running. Without the grace window the streamer reports the same thing,
   and that agreement is the approved outcome. The fix for the drop itself
   (keep last tick's membership when a poll raises) belongs in the reconcile
   tick, and the intent's constraints keep the tick out of this change. The
   follow-up issue is filed before merge so the PR can link it. Nothing gets
   less secure: the streamer stops sooner rather than following longer, and
   `_MAX_EMPTY_REATTACHES` in `build_log_stream.py` is still the backstop.
2. **Q2, CHANGELOG: no entry.** Users see no change in how builds behave. The
   changelog has no entry for the earlier liveness fixes either
   (`grep -n '1662\|1619' CHANGELOG.md` finds nothing). Leaving it out also
   avoids a rebase conflict with open PR #1691.
3. **Q3, land #1670 on its own now.** #1669 (multi-replica and shared state)
   and #1677 (dispatch raises after the Job was created) stay separate. The
   intent's constraints already rule both out, and batching them would only
   make the review bigger.
4. **Scope: what gets deleted so no orphans are left (Ruff F401).** The
   `_DISPATCH_GRACE_SECONDS` block, the `time.monotonic` logic,
   `import time`, and the restart/grace paragraph of the docstring.
5. **Scope: tests.** Drop the grace monkeypatch, turn the "just dispatched"
   test into its inverse, and add two assertions, as described in 2 below.

### 1. `apps/web-server/server/services/agent_kubejob.py`

- **Line 17: delete `import time`.** `grep -n 'time\.'` finds `time` only at
  lines 639 and 644, and both are deleted below. Lines 853 and 979 are
  `datetime.now` and `datetime.fromisoformat`, which don't use the `time`
  module.
- **Lines 32-37: delete the `# #1619: ...` comment and
  `_DISPATCH_GRACE_SECONDS = 45.0`.** The only other reference is
  `tests/test_is_running_kubejob.py:183`, which is removed in 2a.
- **Lines 625-646: make `_kubejob_still_active` a plain membership check.**
  Drop the `started` timestamp. The inner `async def _active()` returns
  `task_id in self._active_kubejob_task_ids` and nothing else. The docstring
  cites #1619 and #1670 and says three things: the answer is the same as
  `is_running` for a kubejob id (membership in the set dispatch marks and the
  tick replaces); the attribute must be read on every call, never captured;
  and an id dispatch never marked is not active, with `_MAX_EMPTY_REATTACHES`
  stopping a dead stream either way. The restart/grace paragraph goes.

  The name, the signature and the async zero-argument closure stay the same,
  so the closure still matches `JobActive = Callable[[], Awaitable[bool]]` at
  `build_log_stream.py:72`. The only caller (line 527) does not change.

  One detail matters here. The closure has to look up
  `self._active_kubejob_task_ids` every time it runs. Line 733 replaces the set
  with a new object (`live | self._kubejob_dispatched_this_tick`) instead of
  changing it in place. A closure that held on to the set from dispatch time
  would keep answering True for the rest of the build, and #1619 would come
  back without anyone noticing. Test 2c below catches that.
- **Lines 520-526 (the comment at the call site) stay as they are.** What they
  say is still true.

### 2. `tests/test_is_running_kubejob.py`

- **a. `test_streamer_liveness_check_follows_the_same_set` (lines 162-186):**
  delete lines 181-183, which are the local
  `import server.services.agent_kubejob as kj`, the monkeypatch and the blank
  line after it. Keep all four asserts. `gone()` is now False with no
  patching.
- **b. Replace lines 189-201 with the inverse test,
  `test_unmarked_id_reads_inactive_to_streamer`.** It uses
  `_service(monkeypatch, [], _StillRunningBackend())` and the id
  `"proj-uuid:999-never-polled"`, then asserts that
  `await service._kubejob_still_active(id)()` is False and that
  `service.is_running(id)` is False. The docstring cites #1670: there is no
  time-based grace, and both readers answer from the same set.
- **c. Stale-capture guard, added to test (a):** build
  `active = service._kubejob_still_active(TASK)` while TASK is live. Then
  make the store return no rows, the way
  `test_finished_kubejob_stops_reading_as_running` does at lines 110-115
  (monkeypatch `_store` to an empty store), and run
  `reconcile_kubejob_builds()` again. Assert that `await active()` is False
  and `is_running(TASK)` is False. In production the streamer's closure is
  built at dispatch and outlives many ticks, and this is the only assertion
  that covers that case.
- **d. Dispatch path, in `test_dispatched_build_is_running_before_any_tick`
  (around line 238):** add
  `assert await service._kubejob_still_active(TASK)() is True`. This shows
  that "a dispatched build streams as today" holds with no grace window and no
  tick.

## Alternatives rejected

1. **Have the closure call `self.is_running(task_id)`.** It would agree with
   `is_running()` automatically, but `agent_service.py:1569` also reads
   `self.running_tasks`, which holds in-pod subprocess builds. That adds a
   second source to a check that only covers kubejobs, which goes against the
   intent ("both read `_active_kubejob_task_ids` only"). It also couples the
   two modules, and mypy would need a `TYPE_CHECKING` declaration on the
   mixin. All of that to save one line.
2. **Inline the check at line 527 and delete `_kubejob_still_active`.** The
   source diff would be a little smaller, but the tests call the method
   directly (lines 175, 180 and 199). Inlining means more test changes and a
   lambda-wrapped coroutine at the call site.
3. **`functools.partial(operator.contains, self._active_kubejob_task_ids,
   task_id)`.** This is wrong twice over. It holds on to the set that line 733
   replaces, and it can't be awaited, so it breaks `JobActive`.
4. **Set `_DISPATCH_GRACE_SECONDS = 0` and keep the branch.** That leaves a
   time-based rule and an unused `time` path in the code. The outcome rules
   this out ("No time-based liveness rule remains").
5. **Keep the previous membership when a poll raises (the Q1 fix).** This
   edits the reconcile tick, which the constraints forbid here. It goes to the
   follow-up issue.
6. **Have the streamer read the store or poll the backend itself.** The
   constraints forbid it, and it would bring back a second source of truth.

## Risks

All of these are in the web-server pod (single replica,
`charts/aifactory/values.yaml:32`). No other host is affected.

- **A transient poll error (accepted, Q1).** When `reconcile_by_poll` raises
  for a row, the id is missing from `live`. The streamer then stops at its
  next end-of-stream. Today the grace window would hide that for up to 45
  seconds. `is_running()` already reports it. The follow-up issue tracks the
  fix.
- **Web-server restart.** The set is empty until the first tick. No streamer
  exists during that time: `_start_kubejob_log_stream` has a single caller,
  dispatch at line 377, which runs after the id is marked at line 371. So the
  grace window protected no path that is still reachable.
- **Dispatch raises after the Job was created (#1677).** The id is never
  marked and no streamer starts. That is today's behaviour, and it is out of
  scope.
- **Deleting too much or too little.** If `import time` stays, Ruff reports
  F401. If it goes while something still uses it, the module fails to import.
  The grep shows lines 639 and 644 are the only uses, so all three are
  deleted together.
- **A later "optimisation" that captures the set.** The live-build streamer
  would never stop. The docstring warns against it, and test 2c fails if it
  happens.
- **`job_active` raising.** A set lookup cannot raise, and
  `build_log_stream.py:318` already suppresses exceptions.

## Verification

```bash
cd /mnt/code/Source-home/GitHub/AIFactory-1670
grep -rn '_DISPATCH_GRACE_SECONDS' apps/ tests/                          # expect: no output
grep -n '^import time\|time\.monotonic' apps/web-server/server/services/agent_kubejob.py   # expect: no output
ruff check apps/web-server/server/services/agent_kubejob.py tests/test_is_running_kubejob.py
ruff format --check apps/web-server/server/services/agent_kubejob.py tests/test_is_running_kubejob.py
pytest tests/test_is_running_kubejob.py -q
pytest tests -q -k "kubejob or log_stream or is_running"
git diff --stat main   # expect: the two files above plus intent/spec/plan only
```

`time.monotonic` is used elsewhere in `apps/` (for example
`server/oidc/userinfo_cache.py`, `services/completion.py`), so that grep is
scoped to `agent_kubejob.py` only.

Done means:

- Both greps print nothing, and Ruff is clean.
- `test_unmarked_id_reads_inactive_to_streamer` passes. The edited
  `test_streamer_liveness_check_follows_the_same_set`, including the
  stale-capture assertion, passes with no monkeypatch. The new assertion in
  `test_dispatched_build_is_running_before_any_tick` passes.
- The existing dispatch-order and `build_log_stream` tests pass without
  changes.
- Mutation check, run by hand once: make the closure hold on to the set, and
  2c fails. Put a grace window back, and 2b fails.
- The Q1 follow-up issue exists and the PR links it.
