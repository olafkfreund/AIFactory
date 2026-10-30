---
status: approved
issue: 1972
spec: spec/2026-10-30-1972-project-scoped-synthetic-key.md
---

# Plan: project-scoped synthetic correlation keys

Decisions (approved spec):

- AIFactory synthetic key `af-<task_id>` with `task_id = <project_uuid>:<spec_id>`.
  `<repo>#<n>` unchanged.
- The build process learns the task id from env `AIFACTORY_TASK_ID`, set by the
  web server on both spawn paths; `parallel_integration` falls back to the old
  `<project_dir.name>:<spec_id>` when it is unset.
- TFactory handoff mirror takes `task_id`; without one it sets no synthetic key
  (never `af-<spec_id>`).
- TFactory synthetic key `tf-<spec_dir.parent.parent.name>:<spec_id>`
  (TFactory layout `workspaces/<project_id>/specs/<spec_id>`).
- CFactory `_upstream_key` AIFactory fallback: `af-<task_id>`; no `task_id` → no key
  (revision 1; never `af-<spec_id>`).
- PFactory: no change. No migration (prod: 0 `af-` rows; 13 `tf-` work items,
  none in flight). No truncation: a synthetic key over 128 chars logs a
  warning and is `None` (revision 1), with a test per repo.

Repos and branches: AIFactory `fix/1972-project-scoped-key` → `dev`;
TFactory `fix/1972-project-scoped-key` → `dev`; CFactory
`fix/1972-af-task-key` → `main`. Steps 1-3 AIFactory, 4 TFactory, 5 CFactory.

## Steps

1. AIFactory `apps/web-server/server/services/completion.py`:
   - `:230` `correlation_key(task_id, issue_number, repo=None)`: synthetic
     `f"af-{task_id}"`; docstring says `af-<project_id>:<spec_id>` (#1972).
   - `:589` log line, `:592`, `:690`, `:762`: pass `task_id` (the builder's
     own `task_id` param) instead of `spec_id`.
   - Module docstring `:11` `af-<spec_id>` → `af-<task_id>`.
   - `apps/web-server/server/routes/from_issue.py:491` pass
     `f"{request.project_id}:{existing_spec_id}"`; `:568` pass `task_id`.
   - `apps/web-server/server/routes/task_service.py:1287` pass
     `f"{task.project_id}:{task.spec_id}"`.
   - Tests: `apps/web-server/tests/test_completion_emitter.py` every
     `af-<spec>` expectation becomes `af-<that event's task_id>` (e.g.
     `af-proj:spec-9`); direct `correlation_key("spec-9", ...)` calls pass a
     task id. Add `test_synthetic_key_differs_across_projects`:
     `correlation_key("p1:003-x", None) != correlation_key("p2:003-x", None)`
     and both `== "af-p1:003-x"` / `"af-p2:003-x"`. Fix
     `tests/test_task_service_module.py:117` if it asserts an `af-` value.
   → verify by `cd apps/web-server && python -m pytest tests/test_completion_emitter.py tests/test_completion_outbox.py -q` and `python -m pytest tests/test_task_service_module.py -q` from the repo root.
   Traps: grep `apps/web-server/tests` and `tests/` for `"af-` and fix every
   hit the change breaks; do not touch `<repo>#<n>` asserts.

2. AIFactory `apps/backend/pfactory/tfactory_client.py`:
   - `:586` `_event_correlation_key(req, task_id: str | None)`: repo-unknown
     case returns `f"af-{task_id}"` if `task_id` else `None`; docstring.
   - `:616` `_thread_issue(contract, req, task_id)` passes it on.
   - `:660` `build_ingest_payload(spec_dir, spec_id, *, contract, task_id: str | None = None)`;
     `:734` passes `task_id`. Docstring: why `None` sets no key.
   - `:1009` `maybe_auto_handoff_tfactory(..., *, contract, task_id: str | None = None)`
     passes it to `build_ingest_payload`.
   - Callers: `apps/web-server/server/routes/execution.py:~927` pass
     `task_id=f"{project_id}:{spec_id}"`;
     `apps/web-server/server/services/completion_orchestration.py:251` and
     `:301` pass `task_id=task_id`.
   - Tests: the pin test at `apps/web-server/tests/test_completion_emitter.py:~1504`
     passes the same task id to both and asserts equality; add a case that
     `_event_correlation_key(req_with_issue_no_repo, None) is None`.
   → verify by step 1's pytest plus `cd apps/backend && python -m pytest pfactory -q` and `python -m pytest tests/test_tfactory_auto_handoff.py tests/test_tfactory_handoff_gitinfo.py -q` from the repo root.
   Traps: `tests/test_tfactory_auto_handoff.py` may assert the exact call
   kwargs of `maybe_auto_handoff_tfactory`; update those to include `task_id`.

3. AIFactory build env:
   - `apps/web-server/server/services/agent_service.py:1292` after the nonce
     line: `env["AIFACTORY_TASK_ID"] = task_id`.
   - `apps/web-server/server/services/build_backend.py:1303` area:
     `extra_env["AIFACTORY_TASK_ID"] = task_id` (unconditional).
   - `apps/backend/agents/parallel_integration.py:200` and `:257`:
     `task_id = os.environ.get("AIFACTORY_TASK_ID") or f"{project_dir.name}:{spec_id}"`,
     `project_id = task_id.split(":", 1)[0]`; pass `task_id=task_id`
     (`:215`, `:270`). Fix the docstring at `:190-193`.
   - Test: in the existing parallel-integration emitter test (grep
     `emit_worker_completion` under `tests/` and `apps/backend`), monkeypatch
     `AIFACTORY_TASK_ID=uuid-1:<spec>` and assert the emitted `task_id`; and a
     build_backend test asserting `extra_env["AIFACTORY_TASK_ID"]` if a
     manifest/env test exists there.
   → verify by the touched tests plus `python scripts/ratchet_lint.py` (if present) and `ruff check` on touched files.
   Traps: `AIFACTORY_TASK_ID` is not a secret; do not add it to
   `core/auth.py` deny lists.

3b. (revision 1) AIFactory length guard:
   - `apps/web-server/server/services/completion.py` `correlation_key` returns
     `str | None`: when the synthetic `af-<task_id>` is over 128 chars,
     `logger.warning(...)` (sanitize_log the task id) and return `None`.
     `<repo>#<n>` is not guarded. Keep callers type-clean (the event builders,
     `from_issue`, `task_service` already tolerate a None value at runtime).
   - `apps/backend/pfactory/tfactory_client.py` `_event_correlation_key`: same
     rule for its `af-<task_id>` (log warning, return `None`).
   - Tests: `correlation_key("p:" + "x" * 130, None) is None` with a warning in
     caplog; the mirror returns `None` for the same task id.
   → verify by step 1 and 2 pytest commands; ruff and the mypy ratchet on
   touched files (`scripts/cq_ratchet.py --tool mypy` as CI runs it).
   Traps: never truncate.

4. TFactory `apps/backend/agents/triager.py:759-760` and
   `apps/backend/agents/handback/send.py:116`: synthetic
   `f"tf-{spec_dir.parent.parent.name}:{spec_id}"` (send.py: `spec_id =
   source.get('spec_id') or spec_dir.name`). Build it in one helper
   `_synthetic_key(spec_dir, spec_id) -> str | None` in `triager.py`, used by
   both (send.py already imports from triager): over 128 chars → log warning,
   return `None` (revision 1). `_correlation_key` return type becomes
   `str | None`. Docstrings say
   `tf-<project_id>:<spec_id>` (#1972, AIFactory#1972).
   Tests: `tests/test_correlation_sync.py:57,80,92,105` and
   `tests/test_triager_completion_webhook.py:264` expect the new form; add
   `test_synthetic_key_differs_across_projects` with spec dirs
   `tmp/workspaces/p1/specs/003-x` and `tmp/workspaces/p2/specs/003-x` for
   both functions, and an over-128 key test returning `None`.
   → verify by `python -m pytest tests/test_correlation_sync.py tests/test_triager_completion_webhook.py tests/test_docs_emit_341.py -q`.
   Traps: none known.

5. CFactory `apps/backend/cfactory/card_intake.py:514-515`: AIFactory body
   without `correlation_key` → `f"af-{body['task_id']}"` when `task_id`, else
   `None` (revision 1: no synthetic key, never `af-<spec_id>`). Docstring `:506-510`.
   Test: `tests/test_card_stage_actions.py:189` parametrize becomes
   `(None, "af-proj-1:003-x")`; add a unit test of `_upstream_key` for two
   bodies with the same `spec_id` and different `task_id` giving distinct keys,
   and a body with only `spec_id` giving `None`.
   → verify by `python -m pytest tests/test_card_stage_actions.py tests/test_card_intake.py -q`.
   Traps: none known.

## Tests

Per step above; then each repo's pre-commit hook and CI. Ratchet locally as CI
does (`scripts/ratchet_lint.py` where present, ruff, mypy on touched files) in
a throwaway venv with the repo's pinned deps.

## Revision 1 (2026-10-30, lead review)

CFactory sets no key without `task_id`; over-long synthetic keys log and
are dropped, never truncated. Spec updated in the same commit.

## Review follow-ups (Opus review of AIF#1978, TF#1477, CF#509)

R1 (HIGH, TFactory) `apps/backend/agents/triager.py` `_synthetic_key`:
   the project id comes from a layout-independent source:
   `status.get("project_id") or source.get("project_id")`, falling back to
   `spec_dir.parent.parent.name` only for the workspaces layout
   (`spec_dir.parent.name == "specs"` and `spec_dir.parent.parent.parent.name == "workspaces"`).
   Any other layout with no id (registered project `<path>/.tfactory/specs/<sid>`,
   a worktree copy) gives no key, with a warning, never `tf-.tfactory:<sid>`.
   `handback/send.py` passes the spec dir's `read_status(spec_dir)` and its
   source.json. Trace: every emitter today (verify stages via
   `verify_pipeline --spec <workspace>`, `routes/handback.py:173`,
   `routes/tfactory_tasks.py` under the workspace root) runs on the
   workspaces layout. Tests: workspaces layout, `.tfactory/specs` with status
   `project_id`, worktree path with source `project_id`, two projects with the
   same spec id giving distinct keys, `.tfactory/specs` without an id giving `None`.
R2 (MEDIUM, AIFactory + TFactory) A `None` key is skipped, not sent
   (CFactory requires `correlation_key: str`, so the outbox would retry for
   about a day and then abandon). AIFactory `services/completion.py`:
   `notify_completion`, `_notify_worker` and `_notify_worker_progress` log a
   warning and do not POST or enqueue when the event has no key
   (`notify_completion` returns True so the fire-once marker is written:
   a retry cannot help). TFactory `triager.py` `_notify_completion` and
   `emit_usage_snapshot`: the same rule before enqueue or POST. One test each.
R3 (MEDIUM, AIFactory) Test that the subprocess spawn env
   (`agent_service.py` near the nonce, `env["AIFACTORY_TASK_ID"]`) carries the
   task id, next to the existing nonce spawn tests in `tests/test_marker_nonce.py`.
R4 (MEDIUM) Deviations recorded below (TFactory extras).
R5 (LOW) Stale docs: AIFactory `completion.py:11` ("never null"; now may be
   absent and is then not sent), `routes/from_issue.py:106`; TFactory
   `handback/send.py:90`, `triager.py:842`.
R6 (LOW) Replace tests that pass on the base: AIFactory
   `test_synthetic_key_differs_across_projects` and
   `test_correlation_key_issue_vs_synthetic` exercise a caller that builds the
   task id (e.g. `build_completion_event` with two `project_id`s / task ids,
   or `task_to_dict` for two projects). TFactory
   `test_triager_completion_webhook.py:264` uses an explicit project id
   (status or a `workspaces/<pid>/specs/<sid>` tree), not one derived from
   `tmp_path`.
R7 (LOW) Log ids safely: CFactory `card_intake.py` uses `sanitize_log`
   (`factory_common.logsafe`); TFactory backend has no `factory_common`, so the
   triager warning logs only the key length, with no untrusted text.

## Deviations

- Step 3b: `build_completion_event`'s `event` dict is annotated
  `dict[str, Any]`; the `str | None` return widened its inferred type and the
  mypy ratchet counted one new error in `completion.py`.
- Step 3: new `tests/test_parallel_integration_task_id.py`; the kubejob env
  assert went into `tests/test_marker_nonce.py`.

- Step 4 (TFactory): `completion_envelope.CompletionEnvelope.correlation_key`
  widened to `str | None`; `docs_emit_trigger` skips the docs emit on `None`;
  `routes/tfactory_tasks.py` omits `correlation_key` when `None`.
- Step 5 (CFactory): the `spec_id` condition on the AIFactory branch is kept,
  so a body with only `task_id` still uses the generic loop (existing tests).
  A 128-char guard was added to `_upstream_key` too.
- R6 (AIFactory): `_sample_worker` in `test_completion_emitter.py` is typed so
  the new typed tests add no mypy errors.
- R1 (TFactory): `tests/test_handback_283.py` and the shared `spec` fixture in
  `tests/test_correlation_sync.py` use a workspaces-layout tree, since a bare
  `tmp_path` now correctly gives no key. R5 also updated the
  `correlation_key` description in
  `apps/backend/contracts/handback-triage-contract.v1.schema.json`.

## Rollback

Revert each PR. Cards created under the new keys stay; old keys were never
written in prod (0 `af-` rows).
