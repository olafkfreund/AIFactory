---
status: draft
issue: 2059
spec: spec/2026-11-17-2059-transcript-hardening.md
coders:
  1: lead
  2: lead
  3: codex   # self-contained: spec runner hooks plus tests, one clear check
  4: lead
reviewers:
  - codex
---

# Plan: Run transcript hardening (items 1, 3, 4, 7 of #2059)

Approved decisions (spec): the transcript key stays the project-qualified `JOB_ID`
(#2080), no new run-id env. (1) dispatch writes `<spec_dir>/transcript_dispatch.json`
`{"since": "<UTC stamp>"}`; completion lists only objects whose file name is
`>= since` and clears `transcriptUris` when none; old objects are kept. (3) spec
Jobs record and upload a transcript under their own `JOB_ID`. (4) SIGTERM handler:
upload in a daemon thread, bounded by `AIFACTORY_TRANSCRIPT_SIGTERM_SECONDS`
(default 20), flush, `os._exit(143)`; no chunked upload. (7) per-process
serialization stays; chart env `AIFACTORY_REPLICAS`, startup warning above 1,
module note, docs.

Stamp format: `datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")`, the prefix of
every transcript file name.

## Steps

1. Item 1. `apps/backend/core/workspace_fetch.py` (`transcript_uris`, ~l.400): add
   `since: str | None = None`; keep keys whose basename is `>= since`.
   `apps/web-server/server/services/completion.py`: add stdlib `mark_transcript_dispatch(spec_dir)`
   (atomic replace of `transcript_dispatch.json`, never raises) and
   `_read_transcript_since(spec_dir)`; `emit_terminal_completion` (~l.1183) passes it
   to `_uris(task_id, since)`; `_record_transcript_uris` (~l.1113) removes
   `transcriptUris` when the list is empty and the key exists.
   `apps/web-server/server/services/build_backend.py` (`dispatch`, after `stamp_spawn`,
   ~l.1408): `mark_transcript_dispatch(spec_dir_for(project_path, spec_id))` via local import.
   Tests: `apps/backend/core/test_workspace_fetch_transcript.py` (since cutoff, names
   just before and after), `apps/web-server/tests/test_completion_emitter.py`
   (marker respected, stale record cleared, no marker unchanged),
   a dispatch test next to the existing build_backend dispatch tests.
   Check: `cd apps/web-server && python -m pytest tests/test_completion_emitter.py -q`
   and `python -m pytest apps/backend/core/test_workspace_fetch_transcript.py -q`.
   Traps: `completion.py` is exec'd standalone (stdlib only, no `server.*` imports);
   ruff alias-sort trap (plain names in parenthesised imports).
2. Item 4. `workspace_fetch.py`: `install_sigterm_upload(spec_id)` as in the spec;
   `apps/backend/cli/main.py` (~l.494) calls it after the `atexit.register`.
   Test in `core/test_workspace_fetch_transcript.py`: subprocess child with a fake
   uploader gets SIGTERM and uploaded before exit 143; a child whose uploader sleeps
   exits 143 within the bound (bound set to 1 s by env).
   Check: `python -m pytest apps/backend/core/test_workspace_fetch_transcript.py -q`.
   Traps: signal handlers only install from the main thread (guard with try/except
   ValueError); handler must not raise.
3. Item 3 (codex). `apps/backend/runners/spec_runner.py` (~l.324, before the
   orchestrator runs): `transcript.start(args.spec_dir)`, `atexit.register(maybe_push_transcripts, <spec dir name>)`,
   `install_sigterm_upload(...)` (the key comes from `JOB_ID`, which the spec Job
   sets; the argument is only the fallback). `apps/backend/spec/pipeline/agent_runner.py`
   (~l.243 and ~l.275): call `task_logger.transcript_tool_start/_end` beside the
   existing `tool_start/_end`, mirroring `agents/session.py:567-615`
   (tool id from `block.id` / `block.tool_use_id`). Tests: a spec_runner test that the
   transcript is started and registered, an agent_runner test that tool events reach
   `transcript.written_files()`.
   Check: `python -m pytest <the new tests> apps/backend -q -k "transcript or spec_runner"`.
   Traps: no secrets in test fixtures; do not change the existing display logging.
4. Item 7. `charts/aifactory/templates/deployment.yaml`: env `AIFACTORY_REPLICAS`
   (`autoscaling.maxReplicas` if `autoscaling.enabled`, else 1 if `rmux.enabled`,
   else `replicaCount`). `apps/web-server/server/main.py` lifespan: a small
   `warn_if_multi_replica()` that logs a warning naming revert idempotency when
   `AIFACTORY_REPLICAS` parses to > 1 (garbage or unset: silent).
   `apps/web-server/server/services/revert_pr.py`: module note on per-process
   serialization. `docs/docs/environment-reference.md`: `AIFACTORY_REPLICAS` and
   `AIFACTORY_TRANSCRIPT_SIGTERM_SECONDS` rows; `.env.example` commented entries;
   `CHANGELOG.md` Unreleased. Tests: warning for 2, silent for 1/unset/garbage;
   `helm template` default, `--set rmux.enabled=true`, `--set autoscaling.enabled=true`.
   Check: `python -m pytest apps/web-server/tests/<new test> -q`; `helm template charts/aifactory | grep -A1 AIFACTORY_REPLICAS`.
   Traps: the autonomy matrix (`docs/docs/compliance/autonomy-matrix.*`) is
   generated: regenerate if CI reports it stale, never hand-merge.

## Tests

`ruff check` and `ruff format --check` on touched files; the pytest files named in
the steps; the repo's pytest job for `apps/backend` and `apps/web-server`.

## Rollback

Revert the PR. The dispatch marker file is ignored without the reader; the added
env var and the signal handler are inert when removed.
