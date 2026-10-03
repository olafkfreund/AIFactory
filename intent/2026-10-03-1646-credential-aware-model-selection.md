---
status: draft
issue: 1646
author: olafkfreund
---

# Intent: a contract never selects a model this environment cannot run

## Problem

A contract can select a model whose provider credential is unusable in the
cluster that will execute it, and the first thing that notices is an agent that
has already spent a phase on it.

Spec `025-myfriends-web-remediation-v2-o` was auto-handed to TFactory. Its
planner died on the first call:

```
Gemini CLI (yolo) error: "API key not valid. Please pass a valid API key."
  (API_KEY_INVALID)
planner: the session never reached a model (no observed model id recorded for
  the planning phase) — not retrying
```

Verification produced nothing.

**This was a deliberate choice, not a leak.** The contract's
`execution.routing.rationale` reads
`tier=medium floor=balanced; class=premium; planning=gemini;
coding=claude-sonnet-4-6; qa=claude-sonnet-4-6; test_gen=claude-sonnet-4-6`.
Traced on PFactory `origin/dev`:

- `apps/backend/plan/emit/cost_router.py:59` — `_ROLES = ("planning",
  "coding", "qa", "test_gen")`; the loop at `:136-160` picks
  `cheapest_capable_model` per role and `apply_cost_routing` (`:197-224`)
  writes the result to `execution.phase_models` and `execution.routing`.
- `:65` — `_MECHANICAL_ROLES = ("coding", "qa", "test_gen")` are restricted to
  the `claude` provider (AIFactory #779). **Only `planning` cost-shops across
  providers**, so `planning` is the one role that can land on gemini.
- The vendored catalog (`model-catalog.json`, byte-identical to the hub's
  `apis/model-catalog.json`) lists `gemini` as `balanced`, metered at
  $1.25/$10 per Mtok — cheaper than `claude-sonnet-4-6` at $3/$15. For a
  `medium` tier (`floor=balanced`) that is the correct pick by
  `cost_router_core._sort_key`.

So the router did what it was designed to do, the contract recorded it, and
AIFactory's handoff carried it to TFactory correctly: `pfactory/tfactory_client.py:626`
merges `{**verify_pm, **execution.phase_models}` so the contract's `planning`
wins. Every layer was truthful. None of them asked whether the cluster can
actually call gemini.

**The credential is present, not absent.** `kubectl` on `k3d-factory` shows
`factory-secrets` carries a 39-byte `GEMINI_API_KEY`, mounted into the
`pfactory`, `aifactory` and `tfactory` deployments alike. The error is
`API_KEY_INVALID`: the key exists and is rejected. A presence check passes.

**What already exists, and why it did not fire.** The issue says "nothing
anywhere checks"; that is almost right, and the gaps are specific:

- `apps/web-server/server/provider_health.py:19` — `provider_credential_health`
  knows gemini, but it is a *presence* check (`GEMINI_API_KEY` non-empty), it
  serves the `/health` endpoint only, and it would have said `configured: true`.
- `apps/backend/core/auth_preflight.py` (#611, RFC-0008 §3.2a) is exactly the
  missing instrument: a live, generation-free credential probe run before a
  build, built for the "present but invalid" case. It **probes anthropic
  only** — `providers_for_models` (`:133-141`) maps `claude*` → `anthropic` and
  nothing else, with "gemini/openai probes are a follow-up" in the comment. It
  defaults to `warn` (never blocks) and is called from one place,
  `cli/build_commands.py:275`, which is the AIFactory build path. The failing
  phase ran in TFactory's verify lane, which has `provider_health.py` but no
  preflight at all.
- `core/provider_failover.py` already classifies a bad credential as "a
  different provider can't fix this" and short-circuits to escalation — it
  handles the failure correctly once it has happened; it does not prevent it.
- A trusted-plan build (`trusted_plan.py:717`, `skip_planning`) never exercises
  the `planning` model in AIFactory. The first process to dial the planning
  provider is TFactory's planner. That is why the AIFactory build succeeded and
  verification was empty.

**The mirror-image fix is parked.** #1638 was diagnosed twice, both wrong, and
led to AIFactory#1645 (draft) which flips the handoff merge so `qa` overrides
`planning`; review showed that clobbers `test_gen` and downgrades the
`governed`-class planner (`cost_router_core.py:197` `planning_floor_class`).
It stays a draft and is not revisited here.

## Proposed outcome

A contract whose selected model cannot be run in the cluster that will execute
it is refused with a named error before any agent phase is spent on it, and the
error names the role, the model and the provider. A contract whose selected
models all resolve to usable credentials behaves exactly as today. The planner
guard that caught this ("never reached a model") keeps its role as the last
line, not the first.

## Affected users and systems

- PFactory `apps/backend/plan/emit/cost_router.py` / `cost_router_core.py` and
  the vendored `model-catalog.json` — the point where the choice is made.
- Factory hub `apis/model-catalog.json` — the canonical catalog PFactory
  vendors; any capability-claim change starts there and is re-vendored.
- AIFactory `apps/backend/core/auth_preflight.py` and its single caller
  `cli/build_commands.py` — the live probe that stops at anthropic.
- AIFactory `apps/backend/pfactory/tfactory_client.py:120-150, 615-635` — the
  handoff that forwards `execution.phase_models` to TFactory.
- TFactory's planner and verify lanes, which is where the phase was spent; the
  `factory-secrets` Secret on `k3d-factory`, shared by all three services.
- Operators reading an empty verification result and a stale #1638 diagnosis.

## Constraints

- Must not reverse the handoff merge precedence or substitute `qa` for
  `planning` — that is #1645's parked mistake and it silently downgrades
  `governed` tasks.
- Must distinguish "no credential" from "credential rejected": the cluster has
  the first class covered and the incident was the second. A fix that only
  checks presence does not fix this incident.
- A probe that cannot reach the provider (network, 5xx) must not block; follow
  `auth_preflight`'s `inconclusive` rule, not a new one.
- Must not change which model is picked when every selected provider is
  usable; the cost router's existing tests and rationale string stay valid.
- The planner guard in TFactory (`agents/planner.py:650-670`) stays; it is the
  backstop, not the thing being replaced.
- Prove it against the live cluster with the current (rejected) key before
  the key is rotated, otherwise the fix is unmeasured.

## Open questions

1. **Which repo owns this?** The issue's three directions land in three
   different places and the answer decides everything downstream:
   - *PFactory router refuses a credential-less provider* — closest to the
     cause, but the router is a pure function over a vendored catalog and has no
     environment to ask; giving it one couples planning to the runtime cluster.
   - *AIFactory refuses at handoff* — `auth_preflight` is already the right
     shape and lives here; extending `providers_for_models` to gemini and
     running it before `build_handoff_payload` is the smallest change that
     stops this incident, and it does not require deciding (1).
   - *Hub catalog becomes environment-aware* — turns "capable" into "capable
     here" for every consumer at once, but a static JSON file cannot know a key
     is rejected, so this can only express presence, not validity.
   My read: (2) in AIFactory is the smallest change that catches this incident,
   and whether PFactory should also refuse is a separate decision the approver
   makes. I am not deciding it here.
2. **Should the probe block by default?** `auth_preflight` ships `warn` because
   an OAuth path cannot be verified offline. A rejected `GEMINI_API_KEY` is a
   definitive 400, so `enforce` for a definitive auth failure may be safe where
   the anthropic probe was not; that is the approver's call.
3. **Where does the handoff probe run?** The handoff happens in the AIFactory
   control plane, which holds the same `factory-secrets` as TFactory; probing
   there is a valid proxy today but stops being one if the two services ever
   get different secrets. Note it, or make TFactory probe on ingest too.
