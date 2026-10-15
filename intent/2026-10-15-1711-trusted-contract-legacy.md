---
status: draft
issue: 1711
author: olafkfreund
---

# Intent: an explicit legacy verdict, and a kubejob check that the pod acts on it

Covers #1711 and #1710 as one task (both are #1673 follow-ups).

## Problem

The web server hands `run.py` its contract verdict in `AIFACTORY_TRUSTED_CONTRACT`
(#1673): a digest when verified, `hold` when held. For `legacy` it sends nothing
(`apps/web-server/server/services/trusted_contract.py:59-64`). The pod
(`apps/backend/core/contract_trust.py:65-89`) cannot tell "the server said
legacy" from "no server at all", so it falls back to its own trace check. The
#1673 spec records the residual gap: an agent that writes a
`context/task_contract.json` with no `approval` key and `change_mode: migration`
in one session gets a migration on the next run (spec/2026-10-10-1673, Risks).

#1673 also has unit tests only. Nothing checks, end to end, that a kubejob
build Job receives the server's verdict and that the pod's build path acts on
that exact value (D11).

## Proposed outcome

- The server always sends a verdict: the digest, `hold`, or `legacy`.
- Under a server `legacy` verdict the pod does not act on a contract file the
  server did not vouch for. A pod run with no server (variable unset) behaves
  as today.
- One test dispatches a kubejob build for each server verdict (verified, hold,
  legacy), reads the value from the Job manifest, and runs the pod's build
  contract resolution with it.

## Affected users and systems

- `apps/web-server` (spawn env for in-pod and kubejob builds).
- `apps/backend` run.py path: `core/contract_trust.py`, prompt contract readers.
- No change for CLI runs with no web server.

## Constraints

- Server and pod ship in one image (the build Job uses the control plane's own
  image, `build_backend.py:127-145`), so both halves change in one PR. An
  operator override `AIFACTORY_BUILD_IMAGE` pointing at an older image must
  still fail closed.
- Never fail open: any doubt holds.
- The variable name and its scrub lists (`core/auth.py:161`,
  `core/child_env.py:23`) stay as they are.

## Open questions

None. Conservative choice taken: under `legacy`, a contract file in the pod is
held (only the signed ingest at `trusted_plan.py:862` writes that file, so an
unsigned one is not legitimate).
