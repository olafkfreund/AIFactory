---
status: approved
issue: 1646
author: olafkfreund
---

# Intent: catch an unusable model credential before a phase is spent

## Problem

Still open on dev at `7bcc9fe5`. Supersedes #1638.

A contract can select a model whose provider credential is rejected, and
nothing notices until an agent has already spent a phase on it. Observed: the
cost router chose `planning=gemini`; the TFactory verify planner then died with
`API key not valid. Please pass a valid API key. (API_KEY_INVALID)`, and
verification produced nothing.

AIFactory has a live credential probe, `apps/backend/core/auth_preflight.py`,
but:

- `providers_for_models` (`:133-140`) maps only `claude*` to `anthropic`, and
  `_PROBES` (`:143`) holds only `probe_anthropic`. A gemini model is never
  probed, on the build path or anywhere else.
- Its one caller is the build path, `apps/backend/cli/build_commands.py:321-340`.
  The TFactory handoff (`pfactory/tfactory_client.py`, `send_handoff` `:741`,
  reached from `maybe_auto_handoff_tfactory` `:927` and the two routes in
  `apps/web-server/server/routes/execution.py:382,945`) runs no preflight, so a
  contract carrying a rejected gemini planner is handed over unchecked.

## Proposed outcome

- A gemini model with a rejected key is reported by name on the build path and
  at handoff, with the provider and the env var, before any agent runs.
- Default mode (`AIFACTORY_AUTH_PREFLIGHT` unset → `warn`) only logs; nothing
  that works today stops working.
- `enforce` mode refuses the handoff on a definite rejection, with a named
  reason, instead of sending it.

## Affected users and systems

AIFactory backend (build CLI, TFactory handoff) and the web server routes that
call the handoff. Operators running with `AIFACTORY_AUTH_PREFLIGHT=enforce`.

## Constraints

- Inconclusive probe results (timeout, 5xx, network, unexpected status) warn
  and never block, in any mode. Only a definite credential rejection may block,
  and only in `enforce`.
- The probe is one cheap, generation-free request with a short timeout.
- The key is never logged and never put in a URL.
- AIFactory only. TFactory's own verify planner preflight is out of scope.
- The probe runs in AIFactory's environment. It cannot see TFactory's; it can
  only catch the case where both share the credential.

## Open questions

None. Cost-router credential awareness (option 1 in the issue) is a separate,
larger change and is not attempted here.
