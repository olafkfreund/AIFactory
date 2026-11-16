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
  under the canonical `spec_dir`, started explicitly by `cli/main.py`
  (`transcript.start`); no-op without `start`. Lock for `--parallel`.
- Each line: `ts, kind (tool_start|tool_end|truncated), tool, phase,
  subtask_id, session, success, payload`. Untruncated.
- Redact before write: `factory_common.secrets.redact` + private-key block
  regex (DOTALL) + exact values of env vars named *TOKEN/KEY/SECRET/PASSWORD*
  (len >= 8). 64 MiB cap checked before the write; one `truncated` line, then
  stop. Never raises.
- Upload: `put_bytes(..., "application/x-ndjson", role="evidence")` to
  `ArtifactRef("aifactory", job_id=spec_id, role="evidence",
  path="transcripts/<file>")`, gated on `S3_ENDPOINT`, local file deleted after
  success, via `atexit` registered in `cli/main.py` (covers build, `--qa`,
  `--followup`, failure, `sys.exit`). 90-day retention is the existing
  `role=evidence` ILM rule: no gitops change.
- Link: `transcript_uris(spec_id)` (`list_artifacts(role_scoped=True)`, filter
  to the `transcripts/` prefix, format `s3://bucket/key`, `[]` on any error or
  without `S3_ENDPOINT`) feeds `transcript_uri` (last) on the completion event
  and `transcriptUris` (all) in `task_metadata.json`, stdlib-only merge in
  `completion.py`.
- Not touched: `core/artifact_store.py` (byte-checked hub copy),
  `spec/pipeline/agent_runner.py`, gitops, spec-creation Jobs.

## Steps

1. `apps/backend/task_logger/transcript.py` (new) and
   `apps/backend/task_logger/test_transcript.py` (new): `start(spec_dir)`,
   `record(kind, tool, payload, **meta)`, `written_files()`, `reset()` (tests).
   Import redaction as `from factory_common.secrets import redact`.
   Verify: `cd apps/backend && python -m pytest task_logger/test_transcript.py -q`
   Traps: ruff and `standards/ruff.toml` disagree on `as` aliases in
   parenthesised imports: use plain names. `record` never raises.
2. `apps/backend/task_logger/logger.py:391` (`tool_start`) and `:443`
   (`tool_end`): add keyword args `raw_input=None` / `raw_result=None`; when not
   `None` call `transcript.record("tool_start"|"tool_end", ...)` with
   `phase_key`, `self.current_subtask`, `self.current_session`, `success` for
   `tool_end`. Pass `raw_input=inp` at `agents/session.py:569`,
   `qa/reviewer.py:244`, `qa/fixer.py:185` and `raw_result=result_content` at
   `agents/session.py:607,625,661`, `qa/reviewer.py:279,310`,
   `qa/fixer.py:220,251`. Verify: `cd apps/backend && python -m pytest
   task_logger agents qa -q -k "not slow"` plus a test in
   `task_logger/test_transcript.py` driving `TaskLogger.tool_start/tool_end`.
   Traps: display truncation stays as is.
3. `apps/backend/core/workspace_fetch.py` (after `maybe_push_task_logs`, ~line
   356): add `maybe_push_transcripts(spec_id)` and `transcript_uris(spec_id)`;
   `apps/backend/cli/main.py` after `debug_success("run.py", "Spec found", ...)`
   (~line 443): `transcript.start(spec_dir)` and
   `atexit.register(workspace_fetch.maybe_push_transcripts, spec_dir.name)`.
   New `apps/backend/core/test_workspace_fetch_transcript.py` (in-memory store
   with `put_bytes`, a paginating `list_artifacts`, unrelated objects present).
   Verify: `cd apps/backend && python -m pytest
   core/test_workspace_fetch_transcript.py core/test_workspace_fetch_task_logs.py -q`
   Traps: do not edit `core/artifact_store.py`; both helpers never raise;
   `list_artifacts` ignores `ref.path`.
4. `apps/web-server/server/services/completion.py`:
   `build_completion_event` (~line 541) gets `transcript_uri: str | None = None`
   and sets `event["transcript_uri"]` when given; `emit_terminal_completion`
   (~line 1084) calls `transcript_uris(spec_id)` inside the existing try, writes
   `transcriptUris` into `task_metadata.json` (stdlib only, non-empty only) and
   passes the last URI. Tests added to
   `apps/web-server/tests/test_completion_emitter.py`.
   Verify: `cd apps/web-server && python -m pytest tests/test_completion_emitter.py
   tests/test_completion_outbox.py -q`
   Traps: `completion.py` is exec'd standalone: no relative or `server.*`
   imports at top level; keep the lazy import of `core.workspace_fetch`.
5. Factory repo (own PR, base `main`): `docs/rfc/0004-governed-trajectory-capture.md`
   status line. Verify: `git diff` shows only the status text.
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
