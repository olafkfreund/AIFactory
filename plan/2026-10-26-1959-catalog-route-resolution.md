---
status: approved
issue: olafkfreund/Factory#1959
spec: spec/2026-10-26-1959-catalog-route-resolution.md
---

# Plan: Resolve contract routes to models from a mounted model catalog

These decisions are carried over from the approved spec:

- **Scope.** Only AIFactory `apps/backend/routing_policy.py` changes. No
  contract, PFactory, gitops or deploy change.
- **Flag.** `AIFACTORY_ROUTE_RESOLUTION=off|shadow|on`, default `off`. Case
  and whitespace are ignored. An unknown value logs a warning and counts as
  `off`.
- **Catalog.** `AIFACTORY_MODEL_CATALOG_PATH`. If it is unset, empty,
  unreadable or invalid, the resolver is inert and returns the
  `DEFAULT_TIERS` model. A bad file also logs a warning. The file is re-read on
  every call, with no cache (`shortcut` comment).
- **Pick.** Lazy-import `core.cost_router_core`'s `load_catalog` and
  `cheapest_capable_model`.
  - Tier to class: `small→cheap`, `mid→balanced`, `frontier→frontier`.
  - Stage to role: `spec→planning`, `qa_fixer→qa`, otherwise the stage itself.
  - Candidates: `provider == "claude"` and `available is not False`.
  - Call: `cheapest_capable_model(role, cls, None, (1, 1), candidates)`.
- **Modes.** `off` returns the default. `shadow` logs the pick at INFO and
  returns the default. `on` returns the pick, or the default if there is none.
- **Where it applies.** It replaces only the `DEFAULT_TIERS` fallback in
  `contract_route`, `policy_route` and `tier_for_model` (the last one only in
  `on`). An explicit policy `tiers` map still wins.

The work is two small code steps in one module plus tests and docs. That is
below the handoff threshold, so Claude implements it directly.

## Steps

1. `apps/backend/routing_policy.py` changes:
   - Next to `ENV_VAR`, add `ROUTE_RESOLUTION_ENV`, `CATALOG_PATH_ENV`,
     `_TIER_CLASS` and `_STAGE_ROLE`.
   - Add `_route_mode()`, `_catalog_model(tier, stage)` and
     `_tier_model(tier, stage)`.
   - In `contract_route` (~L159), use the policy `tiers` if present, otherwise
     `_tier_model(tier, stage)`.
   - In `policy_route` (~L113), change the fallback to
     `_tier_model(tier, stage)`.
   - In `tier_for_model` (~L195), when it falls back to `DEFAULT_TIERS` and the
     mode is `on`, build the map as `{t: _catalog_model(t, "coding") or m}`.

   Verify with `pytest apps/backend/test_routing_policy.py` (the existing
   tests must stay green).
   Traps:
   - The module docstring says stdlib-only. Import `core.cost_router_core`
     lazily, inside `_catalog_model`.
   - Do not edit `core/cost_router_core.py`. The verification-core-drift
     workflow requires it to stay byte-identical to the hub copy.
2. `apps/backend/test_routing_policy.py`: add the seven cases from the spec's
   Verification section. Use a `tmp_path` fixture catalog whose frontier pick
   (`claude-test-frontier`) differs from `opus`. Clear both new env vars in
   the autouse fixture.
   Verify with `pytest apps/backend/test_routing_policy.py -q`. Also confirm
   the `on` test fails if `contract_route` is reverted.
   Traps: the tests run from `apps/backend` (imports are top-level modules).
3. Docs: add the two vars to `docs/docs/environment-reference.md` (Model &
   routing table, after `AIFACTORY_ROUTING_POLICY`) and to
   `apps/backend/.env.example`.
   Verify with `git diff`.
   Traps: none.

## Tests

- `cd apps/backend && .venv/bin/python -m pytest test_routing_policy.py -q`
  → all pass.
- `ruff check` and `ruff format --check` on the touched files → clean.
- `python scripts/ratchet_lint.py`, if it exists → no new findings.

## Rollback

Revert the PR. The flag defaults to `off`, so unsetting
`AIFACTORY_ROUTE_RESOLUTION` (or setting it to `off`) restores the built-in
mapping at runtime without a deploy.

## Deviations

These came up during implementation.

- **Step 1, the lazy import.** The core module is loaded with
  `importlib.import_module("core.cost_router_core")`, not a `from` import. The
  CQ ratchet runs mypy from the repo root, where backend-local packages don't
  resolve. A `from` import added an `import-not-found` and a `no-any-return`,
  which raised `routing_policy.py`'s strict error count above the base.
- **Step 1, `tier_for_model`.** The reverse map is built strongest tier first
  (`reversed(DEFAULT_TIERS.items())`). A floor pick can serve several tiers.
  When it does, the test fixture showed it was stamped `small` instead of its
  own class.
- **Regenerated the autonomy matrix.** The `import_module` call counts as one
  more dynamic import for the modules that reach `routing_policy`.
  `scripts/gen_autonomy_matrix.py` regenerated
  `docs/docs/compliance/autonomy-matrix.md` and
  `docs/static/compliance/autonomy-matrix.json` (CI `--check`).
- **Review fixes (Opus review of #1896).**
  - `_catalog_model` became `_catalog_picks(tiers, stage)`. It loads the
    catalog once per resolution. Loading, filtering and picking are all inside
    one `try/except Exception`, so a malformed entry is inert: the warning
    names the exception type only, never the catalog contents.
  - `tier_for_model` takes an optional `stage`. `agents/token_attribution.py`
    passes the worker's phase, so the reverse map uses the real role.
  - The unknown-mode warning is logged once per process per value.
  - New test: a parseable catalog with malformed entries returns the default
    model in both `shadow` and `on`. It fails against the previous commit.
