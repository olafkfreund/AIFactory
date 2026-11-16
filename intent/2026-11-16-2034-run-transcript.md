---
status: approved
issue: 2034
author: olafkfreund
---

# Intent: store a full per-run transcript (phase 1 of #2034)

## Problem

After a run there is no complete record of what the agent did. The tool-call
log is cut for display (commands to about 50 characters, inputs and results to
about 100, stored detail to 10 KB in `task_logger/logger.py`) and it lives in
`task_logs.json` on local disk (on the Job's emptyDir on the packed path). The
packed path already uploads `task_logs.json`, but that file is the truncated
display log, unredacted and tagged `role=build`. When a merged change
regresses, nobody can reconstruct which commands and edits the run made.

## Proposed outcome

- Every build and QA run (the `cli/main.py` entrypoint) writes an untruncated transcript of its tool
  calls and results as it goes, with secrets redacted before anything is
  written to disk or to the store (`factory_common.secrets.redact`). Each run
  gets its own transcript object, so a retry or a later QA run never overwrites
  an earlier one.
- When the run ends, including a failed run, the transcript is uploaded to the artifact store
  (MinIO, via `core.artifact_store`) and its `s3://` URI is carried in the
  completion event (`transcript_uri`, the latest) and all of a spec's transcript
  URIs are written to the task record (`task_metadata.json`, `transcriptUris`).
- Retention follows the existing object-store ILM: the object carries the
  `role=evidence` tag, which the factory-gitops MinIO rules keep for 90 days.

## Affected users and systems

AIFactory backend (`task_logger`, `agents/session.py`, `qa/*`,
`spec/pipeline/agent_runner.py`, `core/workspace_fetch.py`, `cli/main.py`) and
web-server (`services/completion.py`). Operators reading a run; CFactory reads
the additive event field. Factory `docs/rfc/0004` status line.

## Constraints

- Secrets never reach the store: redact before write, not on read.
- Additive only: the event field is optional, old consumers are unaffected.
- A transcript failure must never fail a build (best-effort). The file has a
  size cap so the in-memory upload stays bounded; past the cap a marker line is
  written and capture stops.
- Known limit: a Job killed before it ends (eviction, OOM) loses its local
  transcript. Streaming upload is a follow-up.
- No gitops change: reuse the `evidence` ILM rule that already exists.
- Out of scope: the automatic revert PR (phase 2), spec-creation Jobs (they
  use a separate push path), and RFC-0004's full governed-capture design
  (signing, hash chain). All go to a follow-up issue.

## Open questions

None. Role `evidence` (90 days) was chosen over `log` (30 days) because the
transcript is the evidence needed after a post-merge regression, which can
surface weeks after the run.
