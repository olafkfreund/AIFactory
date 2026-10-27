---
status: draft
issue: 1929
author: olafkfreund
---

# Intent: authenticate build stdout control markers

## Problem

The control plane reads build stdout for in-band control markers
(`__EXEC_PHASE__:{json}`, `[PHASE_EVENT] k=v`, bare `{"phase"|"status":…}`
JSON lines, `__USAGE__:{json}`) and for rate-limit text. Nothing tells a real
marker from a forged one, and the build prints model text and tool output to
the same stdout unescaped (`agents/session.py:525`, `:604`, `:621`, and the
same `print(block.text)` in qa, spec, ideation and roadmap runners). A model,
or a file or command output it surfaces, can therefore:

- move the task's phase (kanban column, `task_logs.json` phase statuses,
  coding/completed/failed status side effects such as the S3 snapshot);
- write attacker-chosen token/cost figures through `__USAGE__`;
- trigger credential failover by printing "you've hit your limit" anywhere;
- crash the subprocess stdout reader with a malformed line (`{"status":"x"}`
  hits an unbound `old_phase`; `subtask_index=abc` raises in `int()`), after
  which nobody drains the pipe.

Kubejob builds feed pod logs to the same handler, so both backends are affected.

## Proposed outcome

- A phase, usage or rate-limit marker counts only if it carries the per-run
  nonce the control plane handed the build at spawn. Anything else on stdout
  is plain log text.
- Rate-limit failover fires only on the SDK's own rate-limit signal, never on
  model text.
- No single stdout line can stop the reader.

## Affected users and systems

AIFactory control plane (`apps/web-server/server/services/agent_emit.py`,
`agent_service.py`, `agent_spec_creation.py`, `build_backend.py` kubejob
dispatch) and the build process (`apps/backend/core/phase_event.py`,
`core/client.py`, `core/auth.py`). Both build backends (subprocess, kubejob).

## Constraints

- Must work on both backends with no new transport (kubejob only has the pod
  log stream).
- The nonce must never reach the agent's child processes (Claude CLI, tools,
  gate runner test code).
- No change to gitops/cluster config; AIFactory 3.10.11 is mid-deploy.
- Control plane and build image ship together, so no mixed-version window
  beyond a rolling deploy (old builds in flight lose live phase updates until
  they finish; completion is not driven by markers).

## Open questions

None. Design chosen by the lead: per-run nonce, unless a side channel is
smaller (see spec).
