---
status: draft
issue: 1693
intent: intent/2026-10-15-1693-kubejob-keep-membership-on-poll-error.md
---

# Spec: a per-row poll error keeps last tick's membership

## Design

One change in `reconcile_kubejob_builds`
(`apps/web-server/server/services/agent_kubejob.py`), inside the per-row
`except` (`:792-794`): before `continue`, if `job_id` is in the current
`self._active_kubejob_task_ids` (last tick's set, not yet replaced), add it
to `live`.

```python
except Exception:  # noqa: BLE001
    _log.exception("[AgentService] kubejob reconcile failed for %s", job_id)
    if job_id in self._active_kubejob_task_ids:
        live.add(job_id)  # #1693: unknown this tick → keep last tick's answer
    continue
```

The set is still replaced wholesale at `:825`. A row that has left the store
is never iterated, so it still drops out; a row polled terminal still drops
out. Only an unreadable row is carried over, and only for as long as its
store row is still `running` and its poll keeps failing.

No review redrive for that row (`_redrive_kubejob_review` stays on the
success path only): we do not know its state this tick.

Open question 1 is answered conservatively: a row absent last tick stays
absent.

## Alternatives rejected

- **Treat a poll error as "still running" for every row.** Invents a running
  state for a row we never confirmed; that is the drift #1619 removed.
- **Restore the 45 s dispatch grace.** #1670 removed it on purpose (D1).
- **Rely on `_MAX_EMPTY_REATTACHES`.** It is a backstop for a dead stream,
  not a liveness answer for the cockpit or the `/recover` guard.

## Risks

- A row whose poll fails on every tick stays "running" until its store row
  leaves `running`. That matches what the store says, and each failure is
  logged with a traceback, so it is not silent.

## Verification

- New test in `tests/test_is_running_kubejob.py`: tick 1 polls running, tick 2
  the backend raises; `is_running` stays true. A second assertion: a row
  whose first poll raises is not reported running.
- The new test fails on the unfixed code.
- Existing tests in `tests/test_is_running_kubejob.py` and
  `tests/test_agent_kubejob_mixin.py` still pass.
