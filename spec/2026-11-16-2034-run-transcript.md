---
status: draft
issue: 2034
intent: intent/2026-11-16-2034-run-transcript.md
---

# Spec: full per-run transcript in the artifact store

## Design

1. **Capture (Job side).** New `apps/backend/task_logger/transcript.py`. One
   process = one run = one file `<spec_dir>/transcripts/<UTC stamp>-<pid>.jsonl`
   (`spec_dir` of the first `TaskLogger` created in the process; the module
   remembers the files it wrote, so isolated-worktree spec dirs cannot confuse
   the upload). `record(kind, tool, payload, phase)` redacts every string in
   the payload with `factory_common.secrets.redact`, dumps one JSON line
   (`ts, kind, tool, phase, payload`) and appends it. Cap 64 MiB per file; on
   reaching it one `{"kind":"truncated"}` line is written and capture stops.
   Never raises.
2. **Hook.** `TaskLogger.tool_start` / `tool_end` take new optional keyword
   arguments `raw_input` and `raw_result` and call `transcript.record` when
   given. The display truncation stays untouched. The call sites that already
   own the full data pass it: `agents/session.py`, `qa/reviewer.py`,
   `qa/fixer.py`, `spec/pipeline/agent_runner.py` (spec-creation runs record
   locally; upload for them is out of scope).
3. **Upload.** `core/workspace_fetch.maybe_push_transcripts(spec_id)` uploads
   each file this process wrote with `ArtifactStore.put_bytes(key, data,
   "application/x-ndjson", role="evidence")`, key from
   `ArtifactRef("aifactory", job_id=spec_id, role="evidence",
   path="transcripts/<file>")` (correlation `_`, as the other push helpers do).
   `role="evidence"` makes the existing MinIO ILM rule keep it 90 days; no
   gitops change. Gate: `S3_ENDPOINT` set (works on packed and co-mount
   paths; local runs without S3 keep only the local file). Called from the
   `finally` in `cli/main.py` next to `maybe_push_usage`, so failed and
   `sys.exit` runs upload too. Best-effort, never raises.
4. **Link.** `core/workspace_fetch.transcript_uris(spec_id)` lists
   `.../evidence/transcripts/` through `ArtifactStore.list_artifacts` and
   returns sorted `s3://` URIs. `services/completion.emit_terminal_completion`
   calls it (lazy import, inside the existing best-effort try), passes the last
   URI to `build_completion_event(transcript_uri=...)`, which sets the optional
   top-level `transcript_uri` (separate from the RFC-0001a `evidence` block),
   and merges all URIs into `task_metadata.json` as `transcriptUris` (same
   read-merge-write pattern as `tenancy.stamp_spec_tenant`).
5. **Docs.** Factory `docs/rfc/0004` status line says phase 1 (capture, redact,
   store, link) is implemented in AIFactory; revert path and governed capture
   (signing, hash chain) remain proposed. Follow-up issue filed for them.

## Alternatives rejected

- New `role="transcript"`: the ILM rules and `ROLES` enum are in other repos
  (hub copy of `artifact_store.py` is byte-checked); `evidence` already fits.
- `role="log"` (30 days): too short for a regression found weeks later.
- Hooking inside `TaskLogger` only: it receives pre-truncated strings, so the
  full data must come from the call sites.
- Redact on read: secrets would sit in the store.
- Streaming upload during the run: more moving parts; the end-of-run `finally`
  covers failures. A killed Job loses its transcript (documented limit).

## Risks

- Large transcripts: capped at 64 MiB, upload is one in-memory `put_object`.
- Redaction is pattern-based; a secret with no known shape could leak. Same
  limit as every other `redact` user; stated in the PR.
- The `transcript_uri` event field is additive; consumers that validate
  strictly would reject it (`apis/completion-events.asyncapi.yaml` allows extra
  properties).
- `transcripts/` is under `.aifactory/` which is git-ignored, so it is never
  committed to the user's repo.

## Verification

- Unit: `task_logger/test_transcript.py` (redaction of a token in tool input
  and result, untruncated 20 KB value, size cap marker, never-raises).
- Unit: `core/test_workspace_fetch_transcript.py` with an in-memory store
  (key, `role="evidence"`, gate on `S3_ENDPOINT`, list returns URIs).
- Unit: `apps/web-server/tests/test_completion_emitter.py` additions: event
  carries `transcript_uri`, `task_metadata.json` gets `transcriptUris`, event
  unchanged when no transcript exists.
- `ruff`, backend and web-server pytest for the touched modules.
