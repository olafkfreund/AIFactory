---
status: draft
issue: 1960
spec: spec/2026-10-26-1960-acw-autonomy-ceiling.md
---

# Plan: autonomy ceiling and agent identity on `acw_` keys

Approved decisions, carried from the spec:

- Nullable `ApiKey.autonomy_ceiling` (`low`/`medium`/`hard`), no default, plus an alembic migration chained on `5e2d9c1a7b40`.
- Derived `ApiKey.agent_identity = "agent:aifactory/<id>"`, not stored.
- Pure `merge_policy.cap_disposition(decision, ceiling)`: the stricter of the two; `None` passes the decision through; an unknown ceiling gives `hold-blocking`; it never loosens.
- Create request accepts an optional ceiling; responses expose `autonomy_ceiling` and `agent_identity`.
- Out of scope: live merge wiring, and `mcp_stdio/auth.py` (#1885 owns it).

The plan has 3 file-editing steps, but each is a few lines in existing patterns, so it is implemented directly, without a coder handoff.

## Steps

1. `apps/backend/merge/merge_policy.py`: add `cap_disposition` after `tier_permits_auto_merge`, add it to `__all__`, and add a `_DISPOSITION_RANK` map. → verify with `pytest tests/test_merge_policy.py`.
   Traps: reuse `_TIER_ALIASES`; do not restate the tier table.
2. `apps/web-server/server/database/models.py` (`ApiKey`, ~line 378): add the column and the property. New migration `apps/web-server/server/database/alembic/versions/20261026_7a1f3c9e2b50_api_key_autonomy_ceiling.py`. → verify with the single-head check in the new test.
   Traps: house migration docstring style; `down_revision` must be the current head.
3. `apps/web-server/server/routes/api_keys.py`: request field (`Literal`), pass it to `ApiKey(...)`, and response fields filled in by the create and list routes. → verify with `pytest tests/test_api_key_autonomy_ceiling.py`.
   Traps: `sanitize_log` on anything logged; no default value.

## Tests

`cd apps/backend && .venv/bin/python -m pytest ../../tests/test_merge_policy.py ../../tests/test_api_key_autonomy_ceiling.py -q`: all pass. ruff check/format and `scripts/ratchet_lint.py` are clean on the touched files.

## Rollback

Revert the PR. The downgrade migration drops the column. Nothing reads it at runtime outside the key routes.
