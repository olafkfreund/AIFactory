---
status: approved
issue: 2272
intent: intent/2026-11-28-2272-task-list-correlation-key.md
---

# Spec: AIFactory's task list carries the correlation key

## Design

- **Model:** `routes/task_models.py` `Task` (~125) gains
  `correlation_key: str | None = Field(None, description="RFC-0001 correlation key, the same key the completion events carry")`.
- **Fill:** `routes/task_service.py` `spec_to_task` (~997–1051) sets
  `correlation_key=correlation_key(f"{project_id}:{spec_dir.name}", read_issue_number(spec_dir), read_issue_repo(spec_dir), read_contract_key(spec_dir))`.
  These are the helpers `task_to_dict` already imports from `services/completion.py`.
- **Reach:** every `spec_to_task` caller gets the field. The list endpoint returns it, and
  CFactory's adapter reads `correlation_key` first.
- **OpenAPI:** regenerate `apps/web-server/openapi.yaml` with
  `APP_DISABLE_AUTH=true python scripts/generate-openapi-spec.py`.

**Effect on tasks with no issue or contract key:** CFactory now keys them `af-<project>:<spec>`,
matching their completion events. Before, the poller used the bare `<project>:<spec>`. That
mismatch was itself a split.

## Alternatives rejected

- **Compute the key in `list_tasks` only:** the other `spec_to_task` callers that feed views
  would still lack it.
- **Make CFactory call the detail endpoint per task:** that's N extra requests per poll, and it
  only moves the problem.

## Risks

- **The first poll after deploy re-keys existing AIFactory tasks in CFactory**, from the bare id
  to `af-…` or `pf-…`. `prune_duplicate_stages` moves the stage, which is the intended join. Old
  bare-id rows may remain as stale history.

## Verification

- **List endpoint:** a task whose contract key is `pf-337-x` and has no issue returns
  `correlation_key == "pf-337-x"`; an issue task returns `o/r#5`; a plain task returns
  `af-<project>:<spec>`.
- **Consistency:** `spec_to_task` and `task_to_dict` agree on the key for the same spec.
- **OpenAPI:** regenerated, and CI's drift check is green.

Then:
- the existing task route tests pass, the ratchets are clean, and CI is green;
- live: in a P1 run, CFactory's `pf-…` item keeps the AIFactory slice and its usage, and X passes.
