---
status: approved
issue: olafkfreund/Factory#1959
author: olafkfreund
---

# Intent: Resolve contract routes to models from a mounted model catalog

Slice of the model access gateway epic (olafkfreund/Factory#1959). It follows
`intent/2026-10-26-1959-pin-job-provider-creds.md`, whose slice list it uses.
Slices 2–4 need gitops or the owner, so this one comes next: the routes part of
slice 5.

## Problem

The signed Task Contract already carries a route per stage. PFactory writes
`execution.routing.requested` / `overrides` (stage → `small|mid|frontier`), and
AIFactory reads them in `apps/backend/routing_policy.py:contract_route` (#895).
But AIFactory maps that route to a model through `DEFAULT_TIERS`, a dict
hard-coded in the image (`small→haiku`, `mid→sonnet`, `frontier→opus`). The
model catalog that is meant to answer "which model serves this class"
(RFC-0014, `Factory/apis/model-catalog.json`) is also baked into the image.
So changing which model a route means needs a rebuild, re-sign and redeploy.
The epic's AC-2 forbids exactly that.

## Proposed outcome

AIFactory can resolve a route class to a concrete model from a catalog file
read at runtime. The file's path comes from `AIFACTORY_MODEL_CATALOG_PATH`
(later a mounted ConfigMap). A flag controls it:
`AIFACTORY_ROUTE_RESOLUTION=off|shadow|on`.

- `off` is the default and today's behaviour, byte for byte.
- `shadow` logs which model the catalog would pick and changes nothing.
- `on` uses the catalog's model.
- An unknown flag value counts as `off`. So does an unset, unreadable or
  invalid catalog path.

Because the file is re-read on each resolution, editing it changes the route
without a restart.

## Affected users and systems

AIFactory's backend model selection (`routing_policy.py`, reached through
`phase_config.get_phase_model` and `get_phase_model_betas`). It only changes
anything when an operator sets both env vars. There is no gitops change, no
Deployment change and no deploy. PFactory and the contract schema are
unchanged.

## Constraints

- With the flag off or the catalog missing, resolution is unchanged.
- `pinnedModel`, `phaseModels`, the CLI model and an explicit local policy
  `tiers` map keep outranking the catalog. Only the built-in `DEFAULT_TIERS`
  fallback is replaced.
- The RFC-0011 difficulty floor still applies before resolution.
- Reuse the hub router (`core/cost_router_core.cheapest_capable_model`), which
  must stay byte-identical to the hub. Apply PFactory's house rules on top:
  Claude-only for agentic roles (#779/#826), skip `available: false`.
- Do not invent a new route vocabulary. Map the contract's tiers onto the
  catalog's classes (`small→cheap`, `mid→balanced`, `frontier→frontier`).
- No gitops, cluster or credential change, and no deploy.

## Open questions

None for this slice. One pushback is resolved here: the lead's brief asked for
new `route`/`phase_routes` fields in the contract. The contract already has
per-stage routes (`routing.requested/overrides`), so a second field would add a
second vocabulary for the same thing. This slice consumes the existing one.
Moving the routes onto the `local<cheap<balanced<frontier` classes directly is
a contract change for later, if it is still wanted.

## Remaining slices, in order

1. Done (#1886 / TFactory #1447): pin the Job credential set.
2. Gateway exists, nothing uses it: new first-party repo and image, signed,
   Kyverno glob extended, a PASS result plus a deliberate unsigned-tag deny
   (gitops; needs the owner).
3. CFactory through the gateway. Then remove its `OLLAMA_API_KEY` and prove it
   still works.
4. TFactory verify path (Plane A): code before gitops. Drop keys from the
   pinned set as they move.
5. This slice: route → model from a runtime catalog in AIFactory, flag off.
   Follow-ups:
   - a gitops ConfigMap `model-routes`, generated from the hub catalog and
     mounted at `AIFACTORY_MODEL_CATALOG_PATH` (owner);
   - flip to `shadow`, then `on`;
   - the same resolver in the gateway.
6. AIFactory + PFactory (Plane A), including `_PASSTHROUGH_BUILD_ENV`, after one
   full PARR run.
7. Plane B (Claude OAuth), only after the bench test on `ANTHROPIC_BASE_URL` +
   OAuth and the token pool (#670).
8. Revoke and rotate every credential at the provider.
