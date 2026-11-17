---
status: draft
issue: 2059
author: olafkfreund
---

# Intent: Run transcript hardening (items 1, 3, 4, 7 of #2059)

## Problem

#2034/#2054 record a redacted transcript per run and link it from the completion
event. Four gaps remain (numbers follow #2059):

1. A run that dies before its upload leaves an earlier run's transcript URI on
   the completion event and in `task_metadata.json` (`transcriptUris`): the
   listing is by task id and returns every run's objects.
3. Spec-creation Jobs (`runners/spec_runner.py`) record no transcript.
4. A Job killed by SIGTERM (deadline, eviction, cancel) exits without running
   `atexit`, so its transcript is never uploaded.
7. The revert path (`services/revert_pr.py`) is serialized per process only;
   with several control-plane replicas two reverts for one merge can race. This
   is a known limit that is not written down anywhere an operator will see it.

## Proposed outcome

- A completion event only names transcripts written after the current dispatch.
  A run that dies before upload yields no `transcript_uri`, not the previous one,
  and its `transcriptUris` in `task_metadata.json` is cleared at dispatch rather
  than left stale. The key stays the task's `JOB_ID` (project isolation, #2080);
  run isolation within a task comes from a dispatch time, not a new id.
- A spec Job uploads its transcript under its own Job id.
- A Job that gets SIGTERM attempts the transcript upload, best-effort and within
  the termination grace period, before it exits.
- The per-process limit is documented, and the control plane logs a warning at
  startup when more than one replica is configured.

## Affected users and systems

Control plane (`completion.py`, `build_backend.py`, `main.py`), Job code
(`cli/main.py`, `runners/spec_runner.py`, `core/workspace_fetch.py`,
`spec/pipeline/agent_runner.py`), the Helm chart env, operators with replicas > 1.

## Constraints

- No new env contract for the run id: #2080 already keys the transcript by the
  project-qualified `JOB_ID`.
- Transcript upload stays best-effort and never fails a run.
- Secrets stay redacted by the existing `transcript` module; nothing new is logged.
- Chunked upload only if a test shows the SIGTERM upload is insufficient.
- No change to per-process serialization of reverts.

## Open questions

None. Decisions taken under owner delegation 2026-11-16 (recorded on #2059) and
repeated 2026-11-17.
