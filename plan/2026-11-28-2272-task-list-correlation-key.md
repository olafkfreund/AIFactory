---
status: approved
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

Revised after the Codex plan review. Paths are under `apps/web-server/`.

- **Model:** `server/routes/task_models.py` `Task` (~125) gains an optional `correlation_key`
  field (`str | None = None`, with a description).
- **Fill:** `server/routes/task_service.py` `spec_to_task` (~1032) computes
  `key = correlation_key(f"{project_id}:{spec_dir.name}", read_issue_number(spec_dir), read_issue_repo(spec_dir), read_contract_key(spec_dir))`.
  It sets `correlation_key=key` **only when `key` is not `None` and contains neither `/` nor
  `#`**. That covers contract keys like `pf-…` and the synthetic `af-<project>:<spec>`.
  - **Issue keys (`<repo>#<n>`) are left out**, so CFactory keeps today's bare-id row for issue
    tasks. Its single-segment routes (process detail, worker progress, cost routing, the
    live-agent WebSocket) 404 on `/` (Codex review, high). A CFactory follow-up issue tracks the
    route fix.
  - **Overlong synthetic keys:** `correlation_key()` returns `None`, so the list omits the field,
    the adapter uses the bare id, and the event is skipped. This is unchanged from today.
  - **Provenance-only issue tasks:** the issue readers don't see them, so both the list and the
    events use `af-…`. They are consistent.
- **Cost:** three small file reads per spec in the list. This is accepted, because the list
  already reads several files per spec. Optimising it is out of scope.
- **OpenAPI:** regenerate both files, as CI does, from the repo root with the web-server venv:
  `APP_DISABLE_AUTH=true python scripts/generate-openapi-spec.py`, then the same with `--opt-in`.

## Review (plan draft, 2026-11-28)

Codex (gpt-6.2-sol) reviewed read-only, and the worktree was unchanged.

| Finding | Verdict | Change |
|---|---|---|
| `/` in issue keys breaks CFactory single-segment routes (high) | confirmed (CFactory routes_workitems `{correlation_key}`) | issue keys are left out of the list; CFactory follow-up |
| A rollback lets a bare-id poll clear the canonical row's usage (high) | confirmed (`progress.py:178`, `store.py:684`) | stated in Rollback; roll forward instead |
| Provenance-only issue and overlong key cases (medium) | confirmed | behaviour stated, with tests |
| Three extra reads per spec (medium) | confirmed | accepted and noted |
| Regenerate both OpenAPI files; test through the adapter (medium) | confirmed / partly | both files regenerated; the adapter join is checked live in P1, since CFactory is another repo |

## Steps

1. Make the edits and regenerate.

   **Tests** in `tests/test_contract_correlation_key.py`. Extend the `_make_spec` helper:
   - `spec_to_task` gives `pf-337-x` for a contract key with no issue;
   - it gives `None` for an issue task (`o/r#5` is left out), even with a contract key;
   - it gives `af-<pid>:<spec>` for a plain task;
   - it gives `af-…` for a provenance-only issue with a numeric contract key;
   - it equals `task_to_dict(...)["correlationKey"]` whenever that key contains no `/` or `#`;
   - one `list_tasks`-level check that the field is in the serialized list, following an existing
     list test's fixtures (find one with `grep -ln "def test.*list_tasks" tests/`).

   **Verify:**
   ```bash
   cd apps/web-server && PYTHONPATH=../backend <W>/pytest -q -o asyncio_mode=auto tests/test_contract_correlation_key.py <the list test file> tests/test_string_issue_identifier.py
   ```
   Then run ruff, the ratchets after the commit, and `git diff --stat openapi.yaml`, which must
   show only the new field, in both `openapi.yaml` and `openapi-optin.yaml`.

## Tests

- The verify commands above.
- CI green, including the OpenAPI drift check.
- Live: X passes in the P1 run.

## Rollback

Roll forward rather than revert. A revert makes CFactory's next poll key tasks by bare id again, and `prune_duplicate_stages` would then clear the AIFactory stage, with its usage, on the canonical `pf-…` or `af-…` row (Codex review). If a revert is unavoidable, accept that loss for the affected work items.
