---
status: draft
issue: 1693
spec: spec/2026-10-15-1693-kubejob-keep-membership-on-poll-error.md
---

# Plan: a per-row poll error keeps last tick's membership

## Approved decisions (carried from the spec)

- On a per-row `reconcile_by_poll` error, keep the row in `live` only if it
  was in last tick's `_active_kubejob_task_ids`. A row absent last tick stays
  absent (conservative answer to intent question 1).
- The set is still replaced wholesale each tick (#1619); no new state.
- No review redrive on the error path.
- The 45 s dispatch grace stays removed (#1670 D1).

## Steps

1. **Regression test.** File `tests/test_is_running_kubejob.py`. Add a
   `_RaisingBackend` whose `reconcile_by_poll` raises `RuntimeError`, and two
   tests after `test_store_failure_leaves_the_previous_answer_standing`
   (`:139-160`), using the existing `_service` helper (`:52-72`) and `TASK`:
   - `test_poll_error_keeps_last_ticks_membership`: tick with
     `_StillRunningBackend` → `is_running(TASK)` true; swap
     `_build_backend` to `_RaisingBackend`, tick again → still true.
   - `test_poll_error_does_not_invent_membership`: first tick with
     `_RaisingBackend` → `is_running(TASK)` false.
   Check: `pytest tests/test_is_running_kubejob.py -q -o asyncio_mode=auto`
   — the first new test fails before step 2.
   Trap: `_service` binds the backend via `monkeypatch.setattr(service,
   "_build_backend", lambda: backend)`; swap it the same way.

2. **Fix.** File `apps/web-server/server/services/agent_kubejob.py`, the
   per-row `except` at `:792-794` in `reconcile_kubejob_builds`. Before
   `continue`, add:
   `if job_id in self._active_kubejob_task_ids: live.add(job_id)` with a
   one-line `#1693` comment.
   Check: `pytest tests/test_is_running_kubejob.py tests/test_agent_kubejob_mixin.py tests/test_kubejob_review_redrive.py -q -o asyncio_mode=auto`.
   Trap: read the set before line `:825` replaces it — the `except` is
   inside the loop, so it already does.

## Tests

```
pytest tests/test_is_running_kubejob.py tests/test_agent_kubejob_mixin.py tests/test_kubejob_review_redrive.py -q -o asyncio_mode=auto
ruff check apps/web-server/server/services/agent_kubejob.py tests/test_is_running_kubejob.py
ruff format --check apps/web-server/server/services/agent_kubejob.py tests/test_is_running_kubejob.py
```

## Rollback

Revert the commit; behaviour returns to dropping the row for the tick.
