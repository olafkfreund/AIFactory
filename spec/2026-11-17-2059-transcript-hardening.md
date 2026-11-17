---
status: draft
issue: 2059
intent: intent/2026-11-17-2059-transcript-hardening.md
---

# Spec: Run transcript hardening (items 1, 3, 4, 7 of #2059)

## Design

**Item 1, stale URI.** Transcript file names already start with a UTC stamp
(`YYYYMMDDTHHMMSS_ffffffZ-<pid>.jsonl`, `task_logger/transcript.py`), so they sort
by start time. At build dispatch (`KubeJobBuildBackend.dispatch`, next to
`stamp_spawn`) the control plane writes the dispatch time in that stamp format to
its own file, `<spec_dir>/transcript_dispatch.json` (`{"since": "..."}`), by atomic
replace. Only dispatch writes it, so it needs no coordination with the many
read-modify-write users of `task_metadata.json`, which dispatch does not touch.
`emit_terminal_completion` reads it and passes it to
`workspace_fetch.transcript_uris(job_id, since=...)`, which keeps only objects
whose file name is `>= since`. `_record_transcript_uris` (already a writer of
`task_metadata.json`, at completion) now also removes `transcriptUris` when the
list is empty, so a run with no new transcript clears the stale links instead of
leaving them. Old objects stay in the bucket (90-day evidence retention); they are
only not linked. No new env contract: the key stays the project-qualified
`JOB_ID` from #2080. Without the file (tasks dispatched before the upgrade, in-pod
path) behaviour is unchanged.

**Item 3, spec Jobs.** `spec_runner.py` starts the transcript for its spec dir and
registers `maybe_push_transcripts` at exit, exactly as `cli/main.py` does. The key
is the spec Job's own `JOB_ID` (`<task>-spec-<fetch8>`), already in the Job's
scoped S3 prefix list (#2023). `spec/pipeline/agent_runner.py` calls
`task_logger.transcript_tool_start/_end` beside its existing tool logging (the
build session already does). The spec Job's transcript is stored, not linked from
the completion event: the event describes the build.

**Item 4, SIGTERM.** A new `workspace_fetch.install_sigterm_upload()` sets a
SIGTERM handler that runs `maybe_push_transcripts` in a daemon thread, waits at
most `AIFACTORY_TRANSCRIPT_SIGTERM_SECONDS` (default 20, inside the 30 s default
grace), flushes stdout and calls `os._exit(143)`. A hard exit is deliberate: the
process is already being terminated, and unwinding `asyncio.run`, SDK cleanup and
outer `finally` blocks can outlast the grace period. `cli/main.py` and
`spec_runner.py` call it where they start the transcript. The upload is the same
single put as at normal exit (cap 64 MiB). Chunked upload is out unless a test or
measured upload time shows this insufficient.

**Item 7, replicas.** The chart renders `AIFACTORY_REPLICAS`: `autoscaling.maxReplicas` when
autoscaling is enabled (the HPA renders even with rmux, so rmux does not cap it),
else 1 with rmux, else `replicaCount`. The server logs a warning at startup when it is above 1, naming
revert idempotency. `revert_pr.py` gets a module note: serialization is per
process, reconciliation is by a body phrase search, so two replicas can open two
reverts for one merge. Documented in the environment reference.

## Alternatives rejected

- Delete earlier transcript objects at dispatch: destroys evidence the 90-day rule
  keeps. The time filter hides them instead.
- A per-run id env var (`AIFACTORY_RUN_ID`): a new contract between control plane
  and Job; the file-name stamp already orders runs.
- Chunked upload for item 4 now: more moving parts, no evidence the single put
  fails.
- A cross-replica lock for item 7: needs shared state the single-replica chart does
  not have; the owner chose a note and a warning.

## Risks

- Clock skew between control plane and Job works both ways: a Job clock behind
  dispatch by more than the pod start time hides its own transcript; an earlier
  Job's clock far enough ahead could pass the cutoff of a later run that uploads
  nothing. Both need skew larger than pod scheduling plus image start (tens of
  seconds); accepted, since cluster nodes share NTP. The filter is a heuristic for
  "newer than this dispatch", not a proof.
- The SIGTERM path skips normal unwinding (`os._exit`): other atexit work is lost,
  which is fine at termination. A stalled object store costs at most the bound.
- SIGKILL after grace still loses the transcript; accepted.
- Concurrent writers: dispatch writes only its own file; completion's existing
  writer of `task_metadata.json` is unchanged apart from removing one key.

## Verification

- Unit: `transcript_uris` honours `since`, including names just before and after
  the cutoff (both skew directions); dispatch writes the marker; completion omits
  `transcript_uri` and clears `transcriptUris` for a run with no new objects.
- Unit: spec runner starts the transcript; `agent_runner` writes transcript events.
- Subprocess test: a child with a successful fake uploader gets SIGTERM and has
  uploaded before exit 143; a child with an unresponsive uploader exits 143 within
  the bound (set small by env).
- Unit: replica warning fires for `AIFACTORY_REPLICAS=2`, not for 1 or unset.
- `helm template` shows the env (default, rmux, autoscaling); ruff and the pytest
  files touched pass.
