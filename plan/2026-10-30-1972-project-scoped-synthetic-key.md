---
status: draft
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
- CFactory `_upstream_key` AIFactory fallback: `af-<task_id>`, else `af-<spec_id>`.
- PFactory: no change. No migration (prod: 0 `af-` rows; 13 `tf-` work items,
  none in flight). No truncation (keys about 75 chars, column 128).

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

4. TFactory `apps/backend/agents/triager.py:759-760` and
   `apps/backend/agents/handback/send.py:116`: synthetic
   `f"tf-{spec_dir.parent.parent.name}:{spec_id}"` (send.py: `spec_id =
   source.get('spec_id') or spec_dir.name`). Docstrings say
   `tf-<project_id>:<spec_id>` (#1972, AIFactory#1972).
   Tests: `tests/test_correlation_sync.py:57,80,92,105` and
   `tests/test_triager_completion_webhook.py:264` expect the new form; add
   `test_synthetic_key_differs_across_projects` with spec dirs
   `tmp/workspaces/p1/specs/003-x` and `tmp/workspaces/p2/specs/003-x` for
   both functions.
   → verify by `python -m pytest tests/test_correlation_sync.py tests/test_triager_completion_webhook.py tests/test_docs_emit_341.py -q`.
   Traps: none known.

5. CFactory `apps/backend/cfactory/card_intake.py:514-515`: AIFactory body
   without `correlation_key` → `f"af-{body['task_id']}"` when `task_id`, else
   `f"af-{body['spec_id']}"` when `spec_id`. Docstring `:506-510`.
   Test: `tests/test_card_stage_actions.py:189` parametrize becomes
   `(None, "af-proj-1:003-x")`; add a unit test of `_upstream_key` for two
   bodies with the same `spec_id` and different `task_id` giving distinct keys.
   → verify by `python -m pytest tests/test_card_stage_actions.py tests/test_card_intake.py -q`.
   Traps: none known.

## Tests

Per step above; then each repo's pre-commit hook and CI. Ratchet locally as CI
does (`scripts/ratchet_lint.py` where present, ruff, mypy on touched files) in
a throwaway venv with the repo's pinned deps.

## Rollback

Revert each PR. Cards created under the new keys stay; old keys were never
written in prod (0 `af-` rows).
