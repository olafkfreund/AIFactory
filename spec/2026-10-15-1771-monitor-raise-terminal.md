---
status: approved
issue: 1771
intent: intent/2026-10-15-1771-monitor-raise-terminal.md
---

# Spec: a monitor that raises must still record a terminal status

## Design

In the `except Exception as e:` handler of `monitor_process`
(`apps/web-server/server/services/process_monitor.py:909`), first thing, add:

```python
if spec_id and project_path:
    try:
        await service._update_plan_status(
            project_path, spec_id, "failed", task_id, emit_events=False
        )
    except Exception:
        logger.exception("... could not record terminal status for %s", sanitize_log(task_id))
```

Then the existing cleanup and FAILED `_emit_progress` run unchanged.

- Same call, same arguments and same guard as the normal exit (`:784-792`), so
  the control file gets `human_review` + `errors` through the one shared writer.
- `emit_events=False`: the following `_emit_progress(FAILED)` stays the single
  canonical terminal event (Issue #14).
- Placed before the cleanup so `_spec_dirs` etc. are still populated if the
  helper's completion orchestration needs them.
- The write is wrapped so a failing write never skips the cleanup or the event;
  `logger.exception` keeps it loud.

## Alternatives rejected

- Turn the reaper on (`REAPER_DRY_RUN=false`): a deploy change outside this
  lane, and the reaper skips rows-less tasks anyway.
- Write `task_control` directly: duplicates the status mapping and the
  user-`done` guard that `_update_plan_status` already owns.
- A `finally` block: the normal and review paths already write their own status,
  and a second write could overwrite `human_review` from the review branch.

## Risks

- When `spec_id` is unknown (spec creation before a spec dir exists) nothing is
  written, as on the normal path. Accepted: there is no plan to mark.
- If the exception came from `_update_plan_status` itself on the normal path,
  the retry here may raise again; it is caught and logged.

## Verification

New test `tests/test_issue_1771_monitor_raise_terminal.py`: a fake process whose
`wait()` raises `RuntimeError`, a spec with a real plan (phases present) and
control status `in_progress`. After `monitor_process`, `read_control(spec_dir)`
has `status == "human_review"` and `reviewReason == "errors"`, and a FAILED
progress was emitted. It fails on the unfixed code.
