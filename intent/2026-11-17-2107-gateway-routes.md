---
status: approved
issue: 2107
also: 2108
author: olafkfreund
---

# Intent: gateway resolves capability-class routes; AIFactory consumes phase_routes

## Problem

Factory#3801 added `execution.route` / `execution.phase_routes` (class names
`local|cheap|balanced|frontier`) to the task contract, and PFactory emits them
(PFactory#1051). Nothing uses them:

- #2107: the model gateway (`apps/model-gateway/gateway.py`) forwards the request
  body unchanged (l.102-105), so a class name is not a model the upstream knows,
  and it sets no route response header (l.63-67), so PFactory's reader of
  `X-Model-Route` (its signature `model_route`) never sees what actually ran.
- #2108: `phase_config` reads `phaseModels` only; `phase_routes` is dropped when the
  contract becomes `task_metadata.json` (`trusted_plan._EXECUTION_TO_METADATA`).

## Proposed outcome

- A gateway given a routes file maps a request whose `model` is a class name to the
  routed model, and answers with `X-Model-Route: <class>`.
- AIFactory carries `phase_routes` into task metadata and, only when an operator
  turns it on and a gateway is configured, uses `openai-compatible:<class>` as the
  phase model so the gateway resolves it. Otherwise `phase_models` and every other
  source behave exactly as today.
- The class model string reaches the gateway, not another endpoint: when the route
  is active, the OpenAI-compatible config for a class name uses the gateway URL and
  token (not a saved endpoint or `api.openai.com`), and the build Job carries what
  that needs.
- Route precedence is stated: `pinnedModel` and the control-plane fallback first,
  then the phase route (with the RFC-0011 difficulty floor applied to its class),
  then `phaseModels` and the existing order. The beta-header path mirrors it.
- Everything is inert by default: no routes file, no flag, no gateway URL = no change.

Out of scope: the scalar `execution.route` (run-level) is not consumed; only
`phase_routes` is. A contract with only `route` behaves as today.

## Affected users and systems

`apps/model-gateway/gateway.py`, `apps/backend/phase_config.py`,
`apps/backend/trusted_plan.py`, env docs. Operators of the in-house gateway
(factory-gitops mounts the routes file: a follow-up, owner/gitops side).

## Constraints

- Codex review (confirmed): selecting `openai-compatible:<class>` alone would not
  pick the gateway endpoint; the scope above covers it.
- Stdlib only in the gateway; the client token and upstream key are never logged.
- `pinnedModel` and the control-plane fallback still outrank a route.
- No new route vocabulary: the catalog classes only.
- Never invent a model name: a class with no routed model is forwarded unchanged.

## Open questions

None. Decisions taken (owner delegation 2026-11-17), each the least risky option:
routes file is a flat `{class: model}` JSON read per request (hot reload, as the
existing catalog resolver does); consumer preference is an explicit opt-in flag,
because AIFactory cannot ask the gateway whether it resolves a class; one PR
covers both issues since #2108 is blocked on #2107.
