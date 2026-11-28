---
status: draft
issue: 2272
spec: spec/2026-11-28-2272-task-list-correlation-key.md
coders:
  1: manager  # one field + one constructor argument + regen; below the handoff threshold (team policy)
reviewers:
  - codex    # plan review (team policy)
  - codex    # diff review: a provider other than the implementer, Claude (team policy)
---

# Plan: AIFactory's task list carries the correlation key

## Approved decisions

Paths are under `apps/web-server/`.

- **Model:** `server/routes/task_models.py` `Task` (~125) gains an optional `correlation_key`
  field (`str | None = None`, with a description).
- **Fill:** `server/routes/task_service.py` `spec_to_task` (~1032) passes
  `correlation_key=correlation_key(f"{project_id}:{spec_dir.name}", read_issue_number(spec_dir), read_issue_repo(spec_dir), read_contract_key(spec_dir))`.
  The helpers are already imported in this module.
- **OpenAPI:** regenerate `openapi.yaml` with
  `APP_DISABLE_AUTH=true python scripts/generate-openapi-spec.py`, from the repo root, using the
  web-server venv.

## Steps

1. Make the edits and regenerate.

   **Tests** in `tests/test_contract_correlation_key.py`. Extend the `_make_spec` helper:
   - `spec_to_task` gives `pf-337-x` for a contract key with no issue;
   - it gives `o/r#5` for an issue task;
   - it gives `af-<pid>:<spec>` for a plain task;
   - it equals `task_to_dict(...)["correlationKey"]` for the same spec;
   - one `list_tasks`-level check that the field is in the serialized list, following an existing
     list test's fixtures (find one with `grep -ln "def test.*list_tasks" tests/`).

   **Verify:**
   ```bash
   cd apps/web-server && PYTHONPATH=../backend <W>/pytest -q -o asyncio_mode=auto tests/test_contract_correlation_key.py <the list test file> tests/test_string_issue_identifier.py
   ```
   Then run ruff, the ratchets after the commit, and `git diff --stat openapi.yaml`, which must
   show only the new field.

## Tests

- The verify commands above.
- CI green, including the OpenAPI drift check.
- Live: X passes in the P1 run.

## Rollback

Revert the PR. CFactory then keys AIFactory tasks by task id again.
