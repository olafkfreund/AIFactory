---
status: draft
issue: 1670
spec: spec/2026-10-10-1670-log-streamer-dispatch-grace.md
---

# Plan: the log streamer asks the same "is it alive" question as `is_running()`

## Summary of approved decisions

Base: branch `fix/1670-log-streamer-dispatch-grace`, HEAD `e5e0f820`, which
contains `origin/dev` (`1c2df772`, #1691). Every line number below was checked
against that checkout. `origin/dev` is the branch base; diff against it, not
`main`.

The KubeJob log streamer decides whether to reattach after end-of-stream by
calling `_kubejob_still_active(task_id)()` (`agent_kubejob.py`). Today that
closure answers True for any unknown id during a 45 s window after the closure
was built (`_DISPATCH_GRACE_SECONDS`), so it can disagree with `is_running()`.
This change removes the time-based grace so both readers answer from the same
set, `self._active_kubejob_task_ids`, which dispatch marks
(`agent_kubejob.py:371-372`) and each reconcile tick replaces (line 733).

- **D1 (per-row poll error drops the id).** The per-row
  `try/except Exception/continue` around `backend.reconcile_by_poll` at
  `agent_kubejob.py:698-702` (691-695 after this change) leaves the id out of
  `live`, so `is_running()` already says "not running" after a transient poll
  error. Without the grace window the streamer now agrees and stops at its
  next EOF. Accepted. The proper fix (keep last tick's membership when a poll
  raises) edits the reconcile tick, which is out of scope: file a follow-up
  issue before merge and link it from the PR. `_MAX_EMPTY_REATTACHES`
  (`build_log_stream.py:91`) stays the backstop.
- **D2.** No CHANGELOG entry: no user-visible change, #1662/#1619 have none
  either, and it avoids a rebase conflict.
- **D3.** Land #1670 alone. #1669 (multi-replica/shared state) and #1677
  (dispatch raises after the Job was created) stay separate.
- **D4 (no orphans, Ruff F401).** Delete the `_DISPATCH_GRACE_SECONDS` comment
  and constant, the `time.monotonic` logic, `import time`, and the
  restart/grace docstring paragraph. Add no helpers.
- **D5 (tests).** Drop the grace monkeypatch; turn the "just dispatched" test
  into its inverse `test_unmarked_id_reads_inactive_to_streamer`; add a
  stale-capture guard (2c) and a dispatch-path assertion (2d).
- **D6 (files).** Only `apps/web-server/server/services/agent_kubejob.py` and
  `tests/test_is_running_kubejob.py` change. `build_log_stream.py`,
  `agent_service.py`, `job_state_store.py` and `CHANGELOG.md` do not.
- **D7 (closure contract).** Keep the name, the signature
  `def _kubejob_still_active(self, task_id: str) -> Callable[[], Any]` and the
  returned async zero-argument closure, so it still fits
  `JobActive = Callable[[], Awaitable[bool]]` (`build_log_stream.py:72`). The
  only caller (line 527) and its comment (520-526) stay unchanged.
- **D8 (must not capture).** The closure reads `self._active_kubejob_task_ids`
  on every call. It must never hold the set: line 733 rebinds the attribute to
  a new object (`live | self._kubejob_dispatched_this_tick`), so a captured
  set keeps answering True for the rest of the build and silently brings
  #1619 back. The docstring says so and test 2c guards it.
- **D9 (docstring).** Cites #1619 and #1670 and says: (a) the answer equals
  `is_running` for a kubejob id: membership in the set dispatch marks and the
  tick replaces; (b) read the attribute on every call, never capture it,
  because the tick rebinds it; (c) an id dispatch never marked is not active,
  and `_MAX_EMPTY_REATTACHES` stops a dead stream either way.

Rejected (do not implement):

1. Closure calls `self.is_running(task_id)`: adds a second source
   (`self.running_tasks`, `agent_service.py:1569`), couples modules, needs a
   `TYPE_CHECKING` declaration for mypy.
2. Inline the check at line 527 and delete `_kubejob_still_active`: more test
   churn, a lambda-wrapped coroutine at the call site.
3. `functools.partial(operator.contains, set, id)`: captures the set line 733
   replaces, and is not awaitable.
4. `_DISPATCH_GRACE_SECONDS = 0` and keep the branch: a time rule and unused
   `time` path remain.
5. Keep previous membership on poll error: edits the reconcile tick (follow-up
   issue, D1).
6. Streamer reads the store or polls the backend: forbidden, second source of
   truth.

Risks carried over: a transient poll error now stops the streamer at its next
EOF (accepted, D1). A web-server restart is unaffected: the only streamer start
(line 377) runs after the dispatch mark (371). Deleting too much or too little
(only lines 639 and 644 use `time`). A later "optimisation" that captures the
set (guarded by the docstring and 2c). `job_active` cannot raise, and
`build_log_stream.py:318` suppresses exceptions anyway. Single replica
(`charts/aifactory/values.yaml:32`), web-server pod only.

Line shift: deleting line 17 and lines 32-37 moves every later line of
`agent_kubejob.py` up by 7. Old → new: 371/372/377 → 364/365/370; 520-527 →
513-520; 625 (`def _kubejob_still_active`) → 618; 688 → 681; 698-702 →
691-695; 733 → 726. The PR quotes the new numbers.

Environment for every command (run from the worktree root
`/mnt/code/Source-home/GitHub/AIFactory-1670`):

```bash
export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH
```

## Steps

1. `tests/test_is_running_kubejob.py`:181-248: write the tests first (red),
   editing bottom up so earlier line numbers stay valid →
   verify by `python -m pytest tests/test_is_running_kubejob.py -q`
   (expected red: 2b fails and 2a fails at `gone()`; 2c and 2d already pass).
   - **2d, insert after line 248** in
     `test_dispatched_build_is_running_before_any_tick` (237-253), right after
     `assert service.is_running(TASK) is True`:
     `assert await service._kubejob_still_active(TASK)() is True`.
     `_dispatchable` no-ops `_start_kubejob_log_stream` (line 223) and no tick
     runs, so this passes only through the dispatch mark
     (`agent_kubejob.py:371`).
   - **2b, replace lines 188-200** (`test_just_dispatched_build_counts_as_active`;
     decorator at 188, body ends at 200, 201-202 are blank separators) with
     `test_unmarked_id_reads_inactive_to_streamer`. Keep
     `@pytest.mark.asyncio` and the `monkeypatch: pytest.MonkeyPatch`
     parameter. Body: `service = _service(monkeypatch, [], _StillRunningBackend())`,
     id `"proj-uuid:999-never-polled"`, then
     `assert await service._kubejob_still_active(<id>)() is False` and
     `assert service.is_running(<id>) is False`. Docstring cites #1670: there
     is no time-based grace; both readers answer from the same set;
     `_MAX_EMPTY_REATTACHES` is the backstop for a dead stream.
   - **2c, insert after line 185** (end of
     `test_streamer_liveness_check_follows_the_same_set`). Reuse `active`,
     built at line 175 while TASK was live. Add a local class copied from the
     inline pattern at lines 109-115:
     ```python
         # Next tick: the row is gone. The SAME closure must now say False (D8).
         class _EmptyStore:
             async def get_active_kubejobs(self) -> list[dict[str, Any]]:
                 return []

         monkeypatch.setattr(service, "_store", lambda: _EmptyStore())
         await service.reconcile_kubejob_builds()
         assert await active() is False
         assert service.is_running(TASK) is False
     ```
   - **2a, delete lines 181-183** in the same test:
     `import server.services.agent_kubejob as kj`, the blank line, and
     `monkeypatch.setattr(kj, "_DISPATCH_GRACE_SECONDS", 0.0)`. Keep the
     asserts at 176, 177, 184 and 185.

   Traps: keep `_EmptyStore` local (no module-level helper, D4/D5); add no
   imports (`Any` and `pytest` are already imported); run
   `ruff format --check tests/test_is_running_kubejob.py` and
   `ruff check tests/test_is_running_kubejob.py` with the repo default
   config; do not commit yet (step 3), since a tests-only commit is red in CI.

2. `apps/web-server/server/services/agent_kubejob.py`:17, 32-37, 625-646:
   remove the grace window (green), editing bottom up →
   verify by `python -m pytest tests/test_is_running_kubejob.py -q`
   (expect 10 passed) plus the greps in Tests.
   - **625-646, `_kubejob_still_active`:** delete `started = time.monotonic()`
     (639); replace the `_active` body (642-644) with the single line
     `return task_id in self._active_kubejob_task_ids`; replace the docstring
     (626-638), dropping the restart/grace paragraph (630-637), with D9 text.
     Target shape:
     ```python
         def _kubejob_still_active(self, task_id: str) -> Callable[[], Any]:
             """A liveness check for ``KubeJobLogStreamer`` (#1619, #1670).

             Answers exactly what ``is_running`` answers for a kubejob id:
             membership in ``self._active_kubejob_task_ids``, the set dispatch
             marks and each reconcile tick replaces.

             Read the attribute on every call; never capture the set. The tick
             rebinds it to a new object (``live | _kubejob_dispatched_this_tick``),
             so a captured set would keep saying True for the rest of the build.

             An id dispatch never marked is not active. There is no time-based
             grace; ``_MAX_EMPTY_REATTACHES`` stops a dead stream either way.
             """

             async def _active() -> bool:
                 return task_id in self._active_kubejob_task_ids

             return _active
     ```
   - **32-37:** delete the `# #1619: how long after dispatch ...` comment
     (32-36) and `_DISPATCH_GRACE_SECONDS = 45.0` (37). Lines 31 and 38 are
     then two adjacent blank lines before `if TYPE_CHECKING:`; drop one.
   - **17:** delete `import time`. Its only uses were 639 and 644; lines
     846/853/976/979 use `datetime`, not `time`.
   - **Verify only, do not edit** (new numbers): dispatch mark 364/365 and
     only streamer start 370; call-site comment and only caller 513-520;
     `_kubejob_dispatched_this_tick` reset 681; per-row except/continue
     691-695; set rebind 726.

   Traps: missing the `import time` deletion fails Ruff F401; do not capture
   the set in the closure (D8); no new helper and no call to
   `self.is_running` (rejected 1); keep name, signature and closure (D7); no
   CHANGELOG entry (D2); no subprocess spawn is added, so
   `tests/test_no_unscrubbed_spawn.py` does not apply; check `ruff format`
   does not reflow the docstring; `python scripts/gen_autonomy_matrix.py
   --check` must still pass (it does not cite `agent_kubejob.py` lines;
   regenerate only if it fails).

3. Commit, follow-up issue and PR: one code commit covering steps 1 and 2 →
   verify by `git diff --stat origin/dev` (only the two files plus
   intent/spec/plan) and `gh issue view <follow-up>`.
   - Stage both files, run `python scripts/cq_ratchet.py --staged`, then
     commit `fix(kubejob): drop the dispatch grace window from the streamer liveness check (#1670)`
     with the session's attribution trailers.
   - If implementation deviated from this plan, update this file in the same
     commit.
   - Before merge, file the D1 follow-up issue: "kubejob reconcile: keep last
     tick's membership when a per-row poll raises" pointing at
     `agent_kubejob.py:691-695`.
   - PR into `dev` linking intent, spec, plan and the follow-up issue; quote
     the new line numbers (513-520, 618, 726); say which steps the coder did.

   Traps: the commit scope must not contain `#`; branch base is `origin/dev`;
   do not touch `build_log_stream.py`, `agent_service.py`,
   `job_state_store.py` or `CHANGELOG.md` (D6).

## Tests

Baseline at `e5e0f820`: `tests/test_is_running_kubejob.py` 10 passed; the
`-k` subset 143 passed.

```bash
grep -rn '_DISPATCH_GRACE_SECONDS' apps/ tests/                                            # no output
grep -n '^import time\|time\.monotonic' apps/web-server/server/services/agent_kubejob.py  # no output
ruff format --check apps/backend apps/web-server scripts tests                             # no changes needed
ruff check apps/backend apps/web-server scripts tests                                      # All checks passed
python scripts/cq_ratchet.py --staged                                                      # after git add; pass
python -m pytest tests/test_is_running_kubejob.py -q                                       # 10 passed, no grace monkeypatch
python -m pytest tests/test_build_log_stream_reattach.py tests/test_build_log_stream.py -q # all pass, files unchanged
python -m pytest tests -q -k "kubejob or log_stream or is_running"                         # 143 passed
python -m pytest apps/web-server/tests -q -o asyncio_mode=auto                             # same count as origin/dev
python scripts/gen_autonomy_matrix.py --check                                              # pass
git diff --stat origin/dev                                                                 # 2 code files + intent/spec/plan
```

The other tests in `test_is_running_kubejob.py` (including
`test_tick_in_flight_does_not_drop_a_fresh_dispatch` and
`test_failed_dispatch_is_not_marked_running`) and both `build_log_stream`
test files must pass without edits.

One-off mutation checks after step 2; revert each by undoing the edit:

| ID | Break | Must fail |
|----|-------|-----------|
| M1 | Capture the set: `ids = self._active_kubejob_task_ids` before `_active`, closure returns `task_id in ids` | 2c (`assert await active() is False`) |
| M2 | Closure returns `task_id in self._active_kubejob_task_ids or True` (grace that never expires) | 2b, and 2a's `gone()` assert |
| M3 | Closure returns `False` | 2a's `assert await active() is True`, and 2d |
| M4 | Comment out `self._active_kubejob_task_ids.add(task_id)` (line 364) | 2d (and the existing `is_running` assert at 248) |
| M5 | Line 726 mutates in place (`.clear()` then `.update(...)`) | nothing; expected. It shows M1 matters only because of the rebind. Record, not a gate. |

## Rollback

- Before merge: `git checkout origin/dev -- apps/web-server/server/services/agent_kubejob.py tests/test_is_running_kubejob.py`, or drop the branch.
- After merge: `git revert` the single code commit in a PR such as
  `revert(kubejob): restore dispatch grace (#1670)`. It brings back
  `import time`, `_DISPATCH_GRACE_SECONDS = 45.0`, the grace closure and the
  old tests together. No schema, config, chart or data change to undo. Then
  confirm the `-k "kubejob or log_stream or is_running"` subset gives 143
  passed.
- Trigger: streamers that stop at their first EOF while a newly dispatched pod
  is still initialising, which would mean an id reached the streamer before
  dispatch marked it (M4 and 2d say it cannot).
