---
status: draft
issue: 2034
spec: spec/2026-11-16-2034-run-transcript.md
coders:
  1: codex   # small tricky logic: redaction, cap, lock
reviewers:
  - codex    # final diff review against plan
  - agy      # independent second read
---

# Plan: full per-run transcript in the artifact store

Approved spec decisions, carried over so this plan stands alone.
Step 1 is Codex because it is small tricky logic (redaction, cap, lock). Steps
2 to 4 are small wiring edits and stay with the lead; agy has no step (nothing
large and mechanical).

- One process = one run = one `transcripts/<%Y%m%dT%H%M%S_%fZ>-<pid:07d>.jsonl`
  under the canonical `spec_dir`. `transcript.start(spec_dir)` only fixes the
  path in memory; the file is created on the first `record`, so runs that make
  no tool call write and upload nothing. No-op without `start`. Lock for
  `--parallel`.
- Each line: `ts, kind (tool_start|tool_end|truncated), tool, tool_use_id,
  phase, subtask_id, session, success, payload`. Untruncated.
- Redaction order, before write: (1) normalise the payload to JSON primitives
  (`str()` on unknown objects, recursive), (2) private-key block regex (DOTALL),
  (3) values of env vars whose name contains TOKEN/KEY/SECRET/PASSWORD
  (len >= 8, read from `os.environ` at call time, longest first), (4)
  `factory_common.secrets.redact`. The PEM regex must run before `redact`,
  which would otherwise eat the header and leave the body. 64 MiB cap checked
  before the write; one `truncated` line, then stop. Never raises.
- Upload: `put_bytes(..., "application/x-ndjson", role="evidence")` to
  `ArtifactRef("aifactory", job_id=spec_id, role="evidence",
  path="transcripts/<file>")`, gated on `S3_ENDPOINT`, local file deleted after
  success, via `atexit` (covers build, `--qa`, `--followup`, failure,
  `sys.exit`). 90-day retention is the existing `role=evidence` ILM rule.
- Link: `transcript_uris(spec_id)`: prefix is the evidence key of
  `ArtifactRef(..., path="transcripts/x")` without the `x`;
  `list_artifacts(ref, role_scoped=True)`, keep keys with that prefix,
  `sorted`, format `s3://<store.config.bucket>/<key>`; `[]` on any error or
  without `S3_ENDPOINT`. Feeds `transcript_uri` (last) on the event and
  `transcriptUris` (all) in `task_metadata.json`.
- Not touched: `core/artifact_store.py` (byte-checked hub copy),
  `spec/pipeline/agent_runner.py`, gitops, spec-creation Jobs.
- Known gap, stated in the PR: `qa/reviewer.py::_run_tool_fallback` (the
  non-primary-provider QA path) does not use `TaskLogger` and is not captured;
  pattern-based redaction can miss a secret of unknown shape; keys use
  `spec_id` (repeats across projects, #1972) and a run that dies before upload
  leaves the previous run's URI as `transcript_uri`. All go to the follow-up
  issue.

### Deviation from the approved spec (mechanism only)

The spec hooks `TaskLogger.tool_start/tool_end` with `raw_input`/`raw_result`
kwargs. Review found that `session.py`, `qa/reviewer.py` and `qa/fixer.py`
track one scalar `current_tool`, so a message with several tool calls would
log only the first result, and a `None` result would be dropped. Instead
`TaskLogger` gets two methods, `transcript_tool_start(name, tool_input,
tool_use_id, phase)` and `transcript_tool_end(tool_use_id, result, is_error,
phase)`, called at the `ToolUseBlock` and `ToolResultBlock` sites directly;
`transcript.py` maps `tool_use_id` to the tool name. Everything else in the
spec is unchanged, and display logging is untouched.

## Steps

1. `apps/backend/task_logger/transcript.py` (new), `apps/backend/task_logger/test_transcript.py` (new):
   `start(spec_dir)`, `record(kind, tool, payload, **meta)`,
   `tool_start(name, payload, tool_use_id, **meta)`, `tool_end(tool_use_id,
   payload, is_error, **meta)`, `written_files()`, `reset()` (tests only).
   Tests: nested payload and a multi-line private key redacted; env secret
   value redacted; untruncated 20 KB value; `""` and `None` results recorded;
   two starts then two ends pair by id; pre-write cap writes one `truncated`
   line; no-op before `start`; no file when nothing recorded; never raises on
   an unwritable dir.
   Verify: `cd apps/backend && python -m pytest task_logger/test_transcript.py -q`
   Traps: plain names in parenthesised imports (ruff vs `standards/ruff.toml`);
   `from factory_common.secrets import redact`.
2. `apps/backend/task_logger/logger.py` (after `tool_end`, ~line 495): add
   `transcript_tool_start` / `transcript_tool_end` delegating to `transcript`
   with `self.current_subtask`, `self.current_session`, phase. Call sites, each
   one insertion: `agents/session.py` (before `tool_start` at :569, and at the
   top of the `ToolResultBlock` branch, ~:590), `qa/reviewer.py` (:244, ~:267),
   `qa/fixer.py` (:185, ~:208); all inside `if task_logger:` and independent of
   `current_tool`. Add a `TaskLogger` test to `task_logger/test_transcript.py`.
   Verify: `cd apps/backend && python -m pytest task_logger agents qa -q`
   Traps: display truncation and `current_tool` logic stay as they are.
3. `apps/backend/core/workspace_fetch.py` (after `maybe_push_task_logs`, ~:356):
   `maybe_push_transcripts(spec_id)`, `transcript_uris(spec_id)`.
   `apps/backend/cli/main.py` right before `if args.qa:` (~:485, after the
   management commands that return early, so they never register it): import
   `atexit`, `transcript`, `workspace_fetch` at that point, then
   `transcript.start(spec_dir)` and
   `atexit.register(workspace_fetch.maybe_push_transcripts, spec_dir.name)`
   (the later `from core import workspace_fetch` at ~:506 stays valid).
   New `apps/backend/core/test_workspace_fetch_transcript.py` (in-memory store
   with `put_bytes` and a paginating `list_artifacts`; unrelated build and
   evidence objects present; asserts key, `role="evidence"`, gate on
   `S3_ENDPOINT`, local file deleted, sorted `s3://` URIs, `[]` on store error).
   Verify: `cd apps/backend && python -m pytest core/test_workspace_fetch_transcript.py core/test_workspace_fetch_task_logs.py -q`
   Traps: do not edit `core/artifact_store.py`; both helpers never raise.
4. `apps/web-server/server/services/completion.py`: `build_completion_event`
   (~:541) gets `transcript_uri: str | None = None` and sets
   `event["transcript_uri"]` when given. `emit_terminal_completion` (~:1069):
   `uris: list[str] = []` before the existing try; call `transcript_uris`
   inside it; after the try, in its own try/except OSError, merge `uris` into
   `task_metadata.json` as `transcriptUris` (stdlib only, non-empty only); pass
   `uris[-1] if uris else None`. A metadata write failure must not drop the
   URI or the event. Tests in `apps/web-server/tests/test_completion_emitter.py`:
   event carries `transcript_uri`; metadata gets `transcriptUris`; event and
   metadata unchanged when no transcript; metadata write failure still emits.
   Verify: `cd apps/web-server && python -m pytest tests/test_completion_emitter.py tests/test_completion_outbox.py -q`
   Traps: `completion.py` is exec'd standalone: no relative or `server.*`
   imports at top level; keep the lazy import of `core.workspace_fetch`.
5. Follow-up issue (AIFactory) for the revert PR (phase 2), `_run_tool_fallback`
   capture, run id in the key, streaming upload, spec-creation Jobs and
   RFC-0004's governed capture; PR body carries the redaction-limit and
   known-gap notes. Factory repo, own PR on `main`:
   `docs/rfc/0004-governed-trajectory-capture.md` status line says phase 1
   (capture, redact, store, link) is implemented in AIFactory and the rest
   remains proposed.
   Verify: `git diff` in Factory shows only the status text.
   Traps: only after steps 1 to 4 are green.

## Tests

- `cd apps/backend && python -m pytest task_logger core/test_workspace_fetch_transcript.py core/test_workspace_fetch_task_logs.py -q`: all pass.
- `cd apps/web-server && python -m pytest tests/test_completion_emitter.py tests/test_completion_outbox.py tests/test_completion_injection_scan.py -q`: all pass.
- `ruff check` and `ruff format --check` on touched files (repo config).
- `just` / CI commands from `.github/workflows` for the touched trees before push.

## Rollback

Revert the PR. The `transcript_uri` event field and `transcriptUris` metadata
key are additive and ignored when absent; objects already stored expire under
the 90-day `evidence` ILM rule.
