---
status: approved
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
upload bounded by `AIFACTORY_TRANSCRIPT_SIGTERM_SECONDS`
(default 20), flush, `os._exit(143)`; no chunked upload. (7) per-process
serialization stays; chart env `AIFACTORY_REPLICAS`, startup warning above 1,
module note, docs.

The cutoff is a heuristic, not proof (spec Risks): clock skew between control plane
and Job can, in either direction, hide the current transcript or pass an earlier
one; accepted, do not add compensating logic.

Run checks from the repo root with CI's interpreter (`PY=apps/backend/.venv/bin/pytest`;
in a worktree use the main clone's venv path). CI suites: `$PY tests/ -m "not slow" -q`,
`$PY apps/backend -q -o asyncio_mode=auto`,
`$PY apps/web-server/tests -q -o asyncio_mode=auto` with `GRAPHITI_ENABLED=false APP_DISABLE_AUTH=true`.

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
   a dispatch test in `tests/test_build_backend_kubejob.py`. The existing one-argument
   `transcript_uris` mocks in `test_completion_emitter.py` (~l.1610, 1625, 1637) change
   to accept `since`, and a test asserts the cutoff argument (production swallows a
   `TypeError` from a stale mock as an empty list).
   Check: `$PY apps/web-server/tests/test_completion_emitter.py -q -o asyncio_mode=auto`,
   `$PY apps/backend/core/test_workspace_fetch_transcript.py -q -o asyncio_mode=auto`,
   `$PY tests/test_build_backend_kubejob.py -q`.
   Traps: `completion.py` is exec'd standalone (stdlib only, no `server.*` imports);
   ruff alias-sort trap (plain names in parenthesised imports).
2. Item 4. `workspace_fetch.py`: `install_sigterm_upload(spec_id)` as in the spec;
   `apps/backend/cli/main.py` (~l.494) calls it after the `atexit.register`.
   Test in `core/test_workspace_fetch_transcript.py`: subprocess child with a fake
   uploader gets SIGTERM and uploaded before exit 143; a child whose uploader sleeps
   exits 143 within the bound (bound set to 1 s by env).
   Check: `$PY apps/backend/core/test_workspace_fetch_transcript.py -q -o asyncio_mode=auto`.
   Traps: signal handlers only install from the main thread (guard with try/except
   ValueError); handler must not raise.
3. Item 3 (codex). `apps/backend/runners/spec_runner.py` (~l.324, before the
   orchestrator runs): `transcript.start(orchestrator.spec_dir)` (`args.spec_dir` may be None; the
   orchestrator holds the resolved directory), `atexit.register(maybe_push_transcripts, orchestrator.spec_dir.name)`,
   `install_sigterm_upload(orchestrator.spec_dir.name)` (the key comes from `JOB_ID`,
   which the spec Job sets; the argument is only the fallback). `apps/backend/spec/pipeline/agent_runner.py`
   (~l.243 and ~l.275): call `task_logger.transcript_tool_start/_end` beside the
   existing `tool_start/_end`, mirroring `agents/session.py:567-615`
   (tool id from `block.id` / `block.tool_use_id`). Tests: new
   `tests/test_spec_runner_transcript.py` (transcript started on `orchestrator.spec_dir`,
   including a run without `--spec-dir`; atexit registered) and
   `apps/backend/spec/pipeline/test_agent_runner_transcript.py` (tool events reach
   `transcript.written_files()`).
   Check: `$PY tests/test_spec_runner_transcript.py -q` and
   `$PY apps/backend/spec/pipeline/test_agent_runner_transcript.py -q -o asyncio_mode=auto`.
   Traps: no secrets in test fixtures; do not change the existing display logging.
4. Item 7. `charts/aifactory/templates/deployment.yaml`: env `AIFACTORY_REPLICAS`
   (`autoscaling.maxReplicas` if `autoscaling.enabled`, else 1 if `rmux.enabled`,
   else `replicaCount`). `apps/web-server/server/main.py` lifespan: a small
   `warn_if_multi_replica()` that logs a warning naming revert idempotency when
   `AIFACTORY_REPLICAS` parses to > 1 (garbage or unset: silent).
   `apps/web-server/server/services/revert_pr.py`: module note on per-process
   serialization. `docs/docs/environment-reference.md`: `AIFACTORY_REPLICAS` and
   `AIFACTORY_TRANSCRIPT_SIGTERM_SECONDS` rows; `.env.example` commented entries;
   `CHANGELOG.md` Unreleased. Tests: new `apps/web-server/tests/test_replica_warning.py` (warning for 2, silent for
   1/unset/garbage); `helm template` default, `--set rmux.enabled=true`,
   `--set autoscaling.enabled=true`.
   Check: `$PY apps/web-server/tests/test_replica_warning.py -q -o asyncio_mode=auto`; `helm template charts/aifactory | grep -A1 AIFACTORY_REPLICAS`.
   Traps: the autonomy matrix (`docs/docs/compliance/autonomy-matrix.*`) is
   generated: regenerate if CI reports it stale, never hand-merge.

## Deviations

- Step 2: the bound is a SIGALRM timer with the upload run in the main thread, not a
  daemon thread. A worker thread would block on the transcript's RLock when the
  signal lands while the main thread is inside `record()`, losing the upload.

- Final review (codex, round 1), accepted: step 3 also writes every tool result of a
  multi-tool message (`agent_runner.py`, matched by `tool_use_id`, outside the
  `current_tool` guard) and uploads the spec transcript before the `os.execv` build
  handoff (`spec_runner.py`, `atexit` does not run there); step 2's bound variable
  `AIFACTORY_TRANSCRIPT_SIGTERM_SECONDS` joins `_PASSTHROUGH_BUILD_ENV`
  (`build_backend.py`) so it reaches Jobs. Tests added for each.

## Tests

`ruff check` and `ruff format --check` on touched files; the pytest files named in
the steps; then the three CI suites above for the directories touched.

## Rollback

Revert the PR. The dispatch marker file is ignored without the reader; the added
env var and the signal handler are inert when removed.
