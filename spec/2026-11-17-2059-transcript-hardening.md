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
`stamp_spawn`) the control plane calls a new stdlib helper in `completion.py` that
removes `transcriptUris` from `task_metadata.json` and writes `transcriptSince`
(the same stamp format, now). `emit_terminal_completion` reads `transcriptSince`
and passes it to `workspace_fetch.transcript_uris(job_id, since=...)`, which keeps
only objects whose file name is `>= since`. Old objects stay in the bucket (90-day
evidence retention); they are just not linked. No new env contract: the key stays
the project-qualified `JOB_ID` from #2080. Without `transcriptSince` (tasks
dispatched before the upgrade, in-pod path) behaviour is unchanged.

**Item 3, spec Jobs.** `spec_runner.py` starts the transcript for its spec dir and
registers `maybe_push_transcripts` at exit, exactly as `cli/main.py` does. The key
is the spec Job's own `JOB_ID` (`<task>-spec-<fetch8>`), already in the Job's
scoped S3 prefix list (#2023). `spec/pipeline/agent_runner.py` calls
`task_logger.transcript_tool_start/_end` beside its existing tool logging (the
build session already does). The spec Job's transcript is stored, not linked from
the completion event: the event describes the build.

**Item 4, SIGTERM.** A new `workspace_fetch.install_sigterm_upload(spec_id)` sets a
SIGTERM handler that runs `maybe_push_transcripts` and then `sys.exit(143)`;
`cli/main.py` and `spec_runner.py` call it where they register the atexit upload.
The upload is the same single put as at normal exit (cap 64 MiB), well inside the
default 30 s grace. A test delivers a real SIGTERM (`signal.raise_signal`) and
asserts the upload ran. Chunked upload is out unless that test, or a measured
upload time, shows it insufficient.

**Item 7, replicas.** The chart renders `AIFACTORY_REPLICAS` (the effective count:
1 with rmux, else `autoscaling.maxReplicas` when autoscaling is on, else
`replicaCount`). The server logs a warning at startup when it is above 1, naming
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

- Clock skew between control plane and Job: a Job whose clock is behind dispatch by
  more than its pod start time would hide its own transcript. Pod scheduling takes
  longer than typical NTP skew; the filter fails toward showing no link, never a
  wrong one.
- A SIGTERM handler replaces default termination: it must always exit. It does,
  also when the upload raises (`maybe_push_transcripts` never raises).
- SIGKILL after grace still loses the transcript; accepted.

## Verification

- Unit: `transcript_uris` honours `since`; dispatch helper clears and stamps;
  `emit_terminal_completion` omits `transcript_uri` for a run with no new objects.
- Unit: spec runner registers the transcript; handler uploads on a real SIGTERM.
- Unit: replica warning fires for `AIFACTORY_REPLICAS=2`, not for 1 or unset.
- `helm template` shows the env; ruff and the repo's pytest suites for touched
  files pass.
