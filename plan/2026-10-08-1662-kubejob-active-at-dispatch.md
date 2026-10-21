---
status: approved
issue: 1662
spec: spec/2026-10-08-1662-kubejob-active-at-dispatch.md
---

# Plan: a kubejob build counts as running from the moment it is dispatched

This plan carries over every decision from the approved spec. You can
implement it without opening the intent or the spec.

**The bug.** For a kubejob build, `AgentService.is_running(task_id)`
(`apps/web-server/server/services/agent_service.py:1554`) answers from
`self._active_kubejob_task_ids`. Only the reconcile tick writes that set,
replacing it every 15s (`agent_kubejob.py:719`). `_dispatch_build_job` never
adds the task. So for up to 15s after a dispatch, `is_running` is False, and
the recovery guard `_refuse_recovery_while_running`
(`apps/web-server/server/routes/execution.py:981`) lets someone reset a live
Job.

**Decisions approved in the spec:**

1. A successful dispatch adds the task to `_active_kubejob_task_ids`. The add
   goes after the `try/except` around `backend.dispatch`, so a failed dispatch
   is never marked.
2. Race fix. A new set, `_kubejob_dispatched_this_tick`, also gets the task at
   dispatch. `reconcile_kubejob_builds` resets that set to `set()` before its
   first `await`. It then assigns
   `self._active_kubejob_task_ids = live | self._kubejob_dispatched_this_tick`.
   This works because asyncio runs on one thread: the set holds exactly the
   dispatches whose rows this tick's store read could have missed. Each id
   stays for one tick at most, so the #1619 rule (the set is replaced
   wholesale, never grown piece by piece) still holds.
3. `is_running` does not change. It stays synchronous, with no store or
   Kubernetes call.
4. The early return when the store read fails (`agent_kubejob.py:677-681`)
   does not change. The previous set stands.
5. Out of scope, as accepted gaps:
   - the window after a restart, until the first tick finishes;
   - `_DISPATCH_GRACE_SECONDS` and `_kubejob_still_active`, which stay as they
     are;
   - requests served by a replica that did not dispatch (follow-up issue;
     `replicaCount` is pinned to 1).

   Do not touch any of these.
6. Rejected, so do not implement them: merging the old set into the new one,
   a time-based grace period in `is_running`, a forced tick after dispatch,
   and an `asyncio.Lock`.

**Repo traps (all steps):**

- Work in the worktree `/mnt/code/Source-home/GitHub/AIFactory-1662`, on
  branch `fix/1662-kubejob-active-at-dispatch`.
- The venv is at `/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv`,
  not in the worktree. Set
  `V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`.
- The web-server tests need `-o asyncio_mode=auto`.
- Pre-commit runs mypy and pydantic checks and needs the venv on PATH. Commit
  with `PATH="$V:$PATH" git commit -F - <<'MSG' ... MSG`, never with
  `-m` and backticks. End each message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01L5j1hQCdA4NmA4jiKDNBX4
  ```
- Line numbers are from `origin/dev` at `c84f7a03`. Re-grep before editing,
  because step 2 shifts the lines that step 3 uses.
- `agent_kubejob.py` is a mixin. Every new `self.` attribute must also be
  declared in its `if TYPE_CHECKING:` block (`agent_kubejob.py:78-92`), or
  mypy fails.

## Steps

1. **Write the tests first.** Edit `tests/test_is_running_kubejob.py` and
   append the code below. Add `import asyncio` to the imports, next to
   `import sys`. Reuse the existing `_service`, `_noop`, `_StillRunningBackend`
   and `TASK`.

   ```python
   class _DispatchingBackend(_StillRunningBackend):
       """A backend whose dispatch succeeds, or raises when ``fail`` is set."""

       def __init__(self, *, fail: bool = False) -> None:
           self.fail = fail

       async def dispatch(self, **_kwargs: Any) -> str:
           if self.fail:
               raise RuntimeError("k8s api unavailable")
           return "aifactory-build-job"


   def _dispatchable(
       monkeypatch: pytest.MonkeyPatch, backend: _DispatchingBackend, rows: list[str]
   ) -> AgentService:
       service = _service(monkeypatch, rows, backend)
       monkeypatch.setattr(service, "_write_skill_context", lambda *_a, **_k: None)
       monkeypatch.setattr(
           service, "_resolve_claude_token_pooled", lambda *_a, **_k: (None, None, None)
       )
       monkeypatch.setattr(service, "_start_kubejob_log_stream", _noop)
       monkeypatch.setattr(service, "_safe_emit_task_status", _noop)
       return service


   async def _dispatch(service: AgentService) -> None:
       await service._dispatch_build_job(
           task_id=TASK,
           project_path=Path("/nonexistent-project"),
           spec_id="022-some-spec",
           correlation_key=None,
       )


   @pytest.mark.asyncio
   async def test_dispatched_build_is_running_before_any_tick(
       monkeypatch: pytest.MonkeyPatch,
   ):
       """#1662: recovery must refuse a build dispatched seconds ago."""
       from fastapi import HTTPException
       from server.routes import execution

       service = _dispatchable(monkeypatch, _DispatchingBackend(), rows=[])
       await _dispatch(service)

       assert service.is_running(TASK) is True
       with pytest.raises(HTTPException) as exc:
           execution._refuse_recovery_while_running(TASK, service, force=False)
       assert exc.value.status_code == 409
       # force still overrides the guard
       execution._refuse_recovery_while_running(TASK, service, force=True)


   @pytest.mark.asyncio
   async def test_tick_in_flight_does_not_drop_a_fresh_dispatch(
       monkeypatch: pytest.MonkeyPatch,
   ):
       """#1662: a tick that read rows before dispatch must not clear it."""
       service = _dispatchable(monkeypatch, _DispatchingBackend(), rows=[])

       class _GatedStore:
           def __init__(self) -> None:
               self.reading = asyncio.Event()
               self.release = asyncio.Event()

           async def get_active_kubejobs(self) -> list[dict[str, Any]]:
               self.reading.set()
               await self.release.wait()
               return []  # read before set_worker_ref committed

       store = _GatedStore()
       monkeypatch.setattr(service, "_store", lambda: store)
       tick = asyncio.create_task(service.reconcile_kubejob_builds())
       await store.reading.wait()
       await _dispatch(service)
       store.release.set()
       await tick

       assert service.is_running(TASK) is True

       # Bounded: the next tick reads rows without TASK (its Job is gone)
       # and drops it, so the set never only grows.
       class _EmptyStore:
           async def get_active_kubejobs(self) -> list[dict[str, Any]]:
               return []

       monkeypatch.setattr(service, "_store", lambda: _EmptyStore())
       await service.reconcile_kubejob_builds()
       assert service.is_running(TASK) is False


   @pytest.mark.asyncio
   async def test_failed_dispatch_is_not_marked_running(
       monkeypatch: pytest.MonkeyPatch,
   ):
       service = _dispatchable(monkeypatch, _DispatchingBackend(fail=True), rows=[])
       with pytest.raises(RuntimeError):
           await _dispatch(service)
       assert service.is_running(TASK) is False
   ```

   Check: run
   `$V/pytest tests/test_is_running_kubejob.py -q -o asyncio_mode=auto`.

   Expected on `dev`, before any code change:
   - `test_dispatched_build_is_running_before_any_tick` FAILS
     (`is_running` is False).
   - `test_tick_in_flight_does_not_drop_a_fresh_dispatch` FAILS.
   - `test_failed_dispatch_is_not_marked_running` passes. It is a guard for
     step 2, and there is no bug for it to show on `dev`.
   - The 7 existing tests pass.

   Paste the failing output into the step-1 commit message, then commit as
   `test(#1662): pin kubejob is_running from dispatch and across a tick`.

   Traps:
   - If either of the first two tests passes on `dev`, the test is wrong. Stop
     and report back.
   - If a stub misses a dependency of `_dispatch_build_job` (for example,
     `spec_dir_for` touching the disk), stub that dependency. Do not change
     production code in this step.
   - Importing `server.routes.execution` works the same way it does in
     `tests/test_recover_refuses_live_build.py:31`.

2. **Mark the task active at dispatch.** Edit
   `apps/web-server/server/services/agent_kubejob.py`, in
   `_dispatch_build_job`. Find the `except Exception:` block that calls
   `self._release_task_credential(task_id)` and then `raise` (about `:361-365`).
   Directly after that block, and before the
   `# RFC-0017 #680` comment and `await self._start_kubejob_log_stream(` (about
   `:366-370`), insert:

   ```python
           # #1662: the build is live from here, not from the next reconcile
           # tick (≤15s later). Without this, is_running() said False for that
           # window and recovery reset a live Job despite the #1619 guard.
           # After the except block on purpose: a failed dispatch never marks.
           self._active_kubejob_task_ids.add(task_id)
   ```

   Check:
   `$V/pytest tests/test_is_running_kubejob.py -q -o asyncio_mode=auto`.

   Expected:
   - `test_dispatched_build_is_running_before_any_tick` passes.
   - `test_failed_dispatch_is_not_marked_running` passes.
   - `test_tick_in_flight_does_not_drop_a_fresh_dispatch` STILL FAILS, because
     the tick replaces the set. This shows the race is real and needs step 3.

   Commit as `fix(kubejob): mark a build active at dispatch (#1662)`.

   Traps: do not put the add inside the `try` or before `dispatch(...)`.

3. **Close the tick-versus-dispatch race.** This step edits three places.

   a. `apps/web-server/server/services/agent_service.py`, in `__init__`,
      directly after `self._active_kubejob_task_ids: set[str] = set()` (`:107`):

      ```python
              # #1662: ids dispatched since the current reconcile tick began
              # its store read; that tick unions them in so it cannot drop a
              # build whose row it read too early. Reset every tick.
              self._kubejob_dispatched_this_tick: set[str] = set()
      ```

   b. `apps/web-server/server/services/agent_kubejob.py`, in the
      `if TYPE_CHECKING:` block, after `_active_kubejob_task_ids: set[str]`
      (`:83`), add `_kubejob_dispatched_this_tick: set[str]`. In
      `_dispatch_build_job`, directly after the line added in step 2, add
      `self._kubejob_dispatched_this_tick.add(task_id)`.

   c. Same file, in `reconcile_kubejob_builds`:
      - Directly after `if not self._kubejob_backend_enabled(): return out`
        (`:675-676`), and before the `try:` that awaits
        `get_active_kubejobs()`, insert:

        ```python
                # #1662: reset BEFORE the first await. Single-threaded asyncio
                # means every id added from here on was dispatched after this
                # tick's store read began — exactly the rows it may have missed.
                self._kubejob_dispatched_this_tick = set()
        ```

      - Replace `self._active_kubejob_task_ids = live` (`:719`) with:

        ```python
                # #1662: keep builds dispatched while this tick was reading;
                # bounded to one tick because the next tick resets the set.
                self._active_kubejob_task_ids = live | self._kubejob_dispatched_this_tick
        ```

        Leave the #1619 comment above it unchanged.

   Check:
   - `$V/pytest tests/test_is_running_kubejob.py tests/test_recover_refuses_live_build.py -q -o asyncio_mode=auto`:
     all tests pass, including all three new ones.
   - `ruff check apps/web-server/server/services/agent_kubejob.py apps/web-server/server/services/agent_service.py tests/test_is_running_kubejob.py`
   - `ruff format --check` on the same files.

   Commit as
   `fix(kubejob): a tick in flight keeps a fresh dispatch active (#1662)`.

   Traps:
   - The reset must come before the first `await` in the function. If you put
     it after the store read, the race comes back.
   - Do not touch the early-return path at `:677-681`.
   - Do not touch `_kubejob_still_active` or `_DISPATCH_GRACE_SECONDS`.

## Tests

All commands are run from the worktree, with
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`.

```
$V/pytest tests/test_is_running_kubejob.py -v -o asyncio_mode=auto
#   10 passed (7 existing + 3 new)
$V/pytest tests/ -q -o asyncio_mode=auto -k "kubejob or recover or is_running"
#   all pass
ruff check apps/web-server/server/services/ tests/test_is_running_kubejob.py
#   clean
PATH="$V:$PATH" pre-commit run --files apps/web-server/server/services/agent_kubejob.py apps/web-server/server/services/agent_service.py tests/test_is_running_kubejob.py
#   pass (mypy included)
```

Runtime check after deploy: dispatch a build, then within 15 seconds call
`POST /api/tasks/{task_id}/recover` without `force`. Expect a 409 ("still
running"). Before this fix, the task was reset.

## Rollback

Revert the step 2 and step 3 commits with `git revert <sha>`, newest first,
then redeploy. That restores the old behaviour, where a build is marked active
at the first tick. Nothing is persisted or migrated: both sets live in memory,
so there is no data to clean up. The tests from step 1 can stay. If you keep
them, mark the two #1662 tests `xfail` and reference the issue.
