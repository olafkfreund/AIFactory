---
status: draft
issue: 2034
intent: intent/2026-11-16-2034-run-transcript.md
---

# Spec: full per-run transcript in the artifact store

## Design

1. **Capture (Job side).** New `apps/backend/task_logger/transcript.py`.
   `start(spec_dir)` is called once from `cli/main.py` where the canonical
   `spec_dir` is resolved; it fixes the file
   `<spec_dir>/transcripts/<%Y%m%dT%H%M%S_%fZ>-<pid:07d>.jsonl` (fixed width, so
   name order is time order). Without `start()` (tests, spec-creation Jobs,
   other entrypoints) `record` is a no-op. `record(kind, tool, payload, **meta)`
   redacts, dumps one JSON line (`ts, kind, tool, phase, subtask_id, session,
   success, payload`) and appends under a `threading.Lock` (`--parallel`).
   `kind` is `tool_start`, `tool_end` or `truncated`. Never raises.
2. **Redaction before write.** Every string in the payload goes through
   `factory_common.secrets.redact`, plus two additions local to this module
   because `redact` replaces only the private-key header line: a
   `-----BEGIN ... PRIVATE KEY-----` to `-----END ...-----` block regex
   (DOTALL), and exact replacement of the values of environment variables whose
   name contains TOKEN, KEY, SECRET or PASSWORD (length >= 8). Pattern-based, so
   a secret of unknown shape that is not in the environment can still leak
   (stated in the PR).
3. **Cap.** 64 MiB per file, checked before each write: a line that would pass
   the cap is dropped, one `truncated` line is written, capture stops.
4. **Hook.** `TaskLogger.tool_start` / `tool_end` take optional keyword
   arguments `raw_input` and `raw_result` (tested with `is not None`, since `{}`
   and `""` are real values) and call `transcript.record` with the logger's
   phase, subtask, session and, for `tool_end`, `success`. Display truncation is
   untouched. Call sites that own the full data pass it: `agents/session.py`,
   `qa/reviewer.py`, `qa/fixer.py`. `spec/pipeline/agent_runner.py` is left
   alone (spec-creation runs are out of scope).
5. **Upload.** `core/workspace_fetch.maybe_push_transcripts(spec_id)` uploads
   each file `transcript.written_files()` returns with
   `ArtifactStore.put_bytes(key, data, "application/x-ndjson", role="evidence")`,
   key from `ArtifactRef("aifactory", job_id=spec_id, role="evidence",
   path="transcripts/<file>")` (correlation `_`, as the other push helpers do),
   and deletes the local file after a successful upload. `role="evidence"`
   puts it under the existing 90-day MinIO ILM rule; no gitops change. Gate:
   `S3_ENDPOINT` set, so packed and co-mount runs upload and local runs without
   S3 keep the local file. `cli/main.py` registers it with
   `atexit.register(...)` right after `transcript.start`, which covers build,
   `--qa`, `--followup`, failure and `sys.exit` alike (a plain `finally` in the
   build branch would miss the early-returning `--qa` and `--followup`
   branches). Best-effort, never raises.
6. **Link.** `core/workspace_fetch.transcript_uris(spec_id)` returns `[]` unless
   `S3_ENDPOINT` is set and swallows every error. It calls
   `list_artifacts(ref, role_scoped=True)` (that method ignores `ref.path`),
   keeps keys starting with `<evidence prefix>/transcripts/`, sorts them and
   formats `s3://<bucket>/<key>` itself. `emit_terminal_completion` calls it
   inside its existing try, passes the last URI to
   `build_completion_event(transcript_uri=...)`, which sets the optional
   top-level `transcript_uri` (separate from the RFC-0001a `evidence` block and
   independent of `usage`), and merges all URIs into `task_metadata.json` as
   `transcriptUris` with a stdlib-only read-merge-write local to `completion.py`
   (it is exec'd standalone, so it must not import `server.tenancy`), only when
   the list is non-empty.
7. **Docs.** Factory `docs/rfc/0004` status line: phase 1 (capture, redact,
   store, link) implemented in AIFactory; revert path and governed capture
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

- Large transcripts: capped at 64 MiB before write; upload is one in-memory
  `put_object`.
- Keys use `spec_id`, like every other push helper, and `spec_id`s repeat
  across projects (#1972): two projects with the same `spec_id` would share a
  listing. If the last run of a spec dies before upload, `transcript_uri` is
  the previous run's (the file name carries the time). Both go to the follow-up
  issue; a run id in the key is the fix.
- Redaction is pattern-based; a secret with no known shape could leak. Same
  limit as every other `redact` user; stated in the PR.
- The `transcript_uri` event field is additive; consumers that validate
  strictly would reject it (`apis/completion-events.asyncapi.yaml` allows extra
  properties).
- `transcripts/` is under `.aifactory/` which is git-ignored, so it is never
  committed to the user's repo.

## Verification

- Unit: `task_logger/test_transcript.py` (redaction of a token and of a
  multi-line private key in input and result, an env-secret value redacted,
  untruncated 20 KB value, empty-string result recorded, pre-write cap marker,
  no-op before `start`, never-raises).
- Unit: `core/test_workspace_fetch_transcript.py` with an in-memory store
  (key, `role="evidence"`, gate on `S3_ENDPOINT`, local file removed after
  upload, listing filters to the transcripts prefix with unrelated build and
  evidence objects present, returns `s3://` URIs, fake store has a paginator).
- Unit: `apps/web-server/tests/test_completion_emitter.py` additions: event
  carries `transcript_uri`, `task_metadata.json` gets `transcriptUris`, event
  unchanged when no transcript exists.
- `ruff`, backend and web-server pytest for the touched modules.
