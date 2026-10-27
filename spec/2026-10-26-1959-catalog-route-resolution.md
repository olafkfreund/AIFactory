---
status: approved
issue: olafkfreund/Factory#1959
intent: intent/2026-10-26-1959-catalog-route-resolution.md
---

# Spec: Resolve contract routes to models from a mounted model catalog

## Design

All of the change is in `apps/backend/routing_policy.py`, AIFactory's existing
route consumer.

**Two env vars.**

- `AIFACTORY_ROUTE_RESOLUTION`: `off` (default), `shadow` or `on`. Matching
  ignores case and surrounding whitespace. Any other value logs a warning and
  counts as `off`.
- `AIFACTORY_MODEL_CATALOG_PATH`: the path to an RFC-0014 catalog JSON (the
  hub `model-catalog.json` shape, or a bare model map). If it is unset, empty,
  unreadable or not valid JSON, the resolver is inert and returns the default.
  An unreadable or invalid file also logs a warning.

**One resolver, one call point.** `_tier_model(tier, stage)` replaces each
place where the code falls back to the built-in `DEFAULT_TIERS`:

- `contract_route`: used when the local policy has no `tiers` map;
- `policy_route`: the `or DEFAULT_TIERS.get(tier)` fallback;
- `tier_for_model`: the reverse map, used only when there is no policy `tiers`
  map and the mode is `on`, so the `routing_tier` stamp still matches the model
  that actually ran.

An explicit local policy `tiers` map still wins over the catalog, just as it
wins over `DEFAULT_TIERS` today. Precedence in `phase_config` is untouched:
pinnedModel > phaseModels > CLI > contract route > policy route > metadata.model > default.

**What each mode does.**

- `off`: returns `DEFAULT_TIERS[tier]`. The catalog is never read.
- `shadow`: picks from the catalog, logs at INFO `stage, tier → catalog pick
  (default X)`, and returns the default.
- `on`: returns the catalog pick. If there is no pick, it returns the default.

**How the catalog picks.** The lazy import
`core.cost_router_core.load_catalog` / `cheapest_capable_model` is the hub
router, unchanged.

- Tier to class: `small→cheap`, `mid→balanced`, `frontier→frontier`. This is
  RFC-0014 §2b, already written in the `DEFAULT_TIERS` comment.
- Stage to role: `spec→planning`, `qa_fixer→qa`; any other stage is used as is.
- Candidates are filtered the way PFactory's `cost_router.select_phase_models`
  does: `provider == "claude"` (#779/#826) and `available is not False`.
- The call is `cheapest_capable_model(role, class, None, (1, 1), candidates)`.
  Any positive token estimate works, because only the price ratio orders
  metered models. With (0, 0), every price would tie and the ids would decide.
- The file is re-read on every resolution, so a ConfigMap update takes effect
  without a restart.
  `shortcut: no caching; add an mtime cache if profiling shows the read.`

**No contract change.** The contract's existing `routing.requested/overrides`
are the routes. PFactory and the `phase_models` handling are unchanged.

**Docs.** Add both vars to `docs/docs/environment-reference.md` (model and
routing table) and `apps/backend/.env.example`.

## Alternatives rejected

- **New `route`/`phase_routes` contract fields (the lead's brief).** The
  contract already has a route per stage (`routing.requested/overrides`, #895).
  A second field would mean a second vocabulary and a PFactory + schema change
  for no new capability.
- **Resolve in `phase_config._resolve_phase_model`.** Routes are resolved in
  `routing_policy`, and `get_phase_model_betas` and `tier_for_model` both go
  through it. Resolving in `phase_config` would mean three call sites instead
  of one.
- **Exact class match with our own tie-break.** That would be a second
  selection policy, beside the hub router that PFactory already uses for
  `phase_models`.
- **Read `budget_enforcement.DEFAULT_CATALOG_PATH` (the baked catalog).** This
  is the coupling AC-2 forbids. The catalog must come from the mounted path.
- **Cache the catalog.** Not needed at this call rate, and it would add
  hot-reload invalidation code.

## Risks

- Under `on`, the pick depends on the mounted catalog. The AIFactory and hub
  catalogs do not mark superseded models `available: false` the way PFactory's
  does. A catalog like that resolves `frontier` to `claude-opus-4-8` and `mid`
  to `claude-sonnet-4-6`, because prices tie and the ids break the tie.
  `shadow` exists to show this before `on`. The gitops ConfigMap (a later
  slice) must carry the `available` flags.
- A pick that `MODEL_ID_MAP` does not know passes through `resolve_model_id`
  as a full id. The model registry checks it (advisory by default).
- Nothing changes at runtime unless an operator sets both vars. No host or
  deploy is involved.

## Verification

Add to `apps/backend/test_routing_policy.py`:

1. `off` (unset, and set to `off`) with a catalog present: the contract route
   resolves to the `DEFAULT_TIERS` model.
2. `shadow`: same model as `off`, plus a log record that names the catalog
   pick.
3. `on`: a fixture catalog whose frontier pick differs from the default.
   `get_phase_model` returns the catalog model.
4. `on` with the path unset, missing or invalid: default model.
5. An unknown mode value (`bogus`): default model and a warning.
6. `on`: `tier_for_model(catalog_pick, metadata)` returns the tier.
7. `on` with an explicit policy `tiers` map: the policy wins.

Run `pytest apps/backend/test_routing_policy.py`, plus ruff and the mypy
ratchet on `routing_policy.py`.
