---
status: approved
issue: 2037
intent: intent/2026-11-16-2037-spec-job-followups.md
---

# Spec: spec Job follow-ups

## Design

1. **Plan progress keys** (`workspace_fetch._checked_spec_json`): drop the
   top-level `status`, `planStatus`, `reviewReason` from a fetched plan and set
   every subtask `status` to `pending`. `fetch_spec_files` then copies those
   three top-level keys back from the local plan when one exists, so a status
   the control plane wrote while the Job ran survives.
2. **Failed plan read** (`fetch_spec_files`): new optional `failed: list[str]`
   out-argument. A read error that is not a missing key (`NoSuchKey`, `404`,
   `NotFound`), or a rejected file, adds the name. `_finish_spec_fetch` returns
   1 and invalidates approval when `implementation_plan.json` is in `failed`,
   also on a first run with no local plan.
3. **Local deadline** (`SpecJobProcess`): `deadline` seconds (default 7500, the
   Job's 7200 s `activeDeadlineSeconds` plus 300 s grace). Each poll and the delete
   call is bounded by 30 s, so a stalled API call cannot skip the check. Past
   it the poller deletes the Job, sets returncode 1 and releases the credential.
4. **Stop during fetch-back**: a `threading.Event` (`cancel`) set by
   `terminate`/`kill`/`send_signal`. `fetch_spec_files` checks it before each
   object and before each write; `_finish_spec_fetch` then returns 1. Stop waits
   for at most one object read; at most one file already past the check may
   still be replaced (the check and the replace are not atomic).
5. **Log buffering**: each line is cut to 16000 characters (under the 64 KiB
   reader limit even at 4 bytes per character) and dropped while more than
   1 MiB is unread.
6. **0600**: fetched files are chmod 0644, as the subprocess path creates them.
7. **Tests**: `monitor_process` with a pid-less process and return code 0; the
   short read through `fetch_spec_files`.

## Alternatives rejected

- Keeping Job subtask status: a Job that ran to completion would mark the plan
  built.
- Bounding the log reader with a custom queue: more code; truncate and drop is
  enough for a log view.
- Bounding the aiohttp source read in `build_log_stream`: separate module, out
  of scope; only the consumer side is fixed here.
- Cancelling the S3 thread: not possible; checking between objects is.

## Risks

- Subtask progress inside a fetched plan is reset to `pending`. A spec session
  makes no progress, so nothing is lost.
- A dropped log line is silent past the cap; the Job outcome is unaffected.
- The reader's `_buffer` is a private attribute (comment marks the shortcut).

## Verification

`pytest tests/test_spec_job_backend.py`, ruff check and format on touched files.
