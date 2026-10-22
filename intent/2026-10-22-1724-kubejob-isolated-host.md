---
status: draft
issue: 1724
author: olafkfreund
---

# Intent: kubejob is the supported isolated host for trusted plans

Covers #1724 and #1760.

## Problem

`host_isolated()` (`apps/web-server/server/services/trusted_contract.py`)
counts only the kubejob backend with the durable job store as isolated. Its
comment reads as a temporary state ("widen once ...") and #1760 asks whether
a PID-namespaced sandbox should now count. #1724 asks how to close the
`/proc/<child>` window: the per-call GitHub token in `hosts.yml` is 0600, but
an agent running under the same uid can read it through `/proc/<child>` on
non-kubejob hosts. Neither question has a recorded answer, and operators have
no doc telling them which host a trusted-plan build needs.

## Proposed outcome

The owner's decision is recorded: kubejob with the durable store stays the
only isolated host; the PID-namespaced sandbox is not widened in. The code
comment states this as policy and links both issues, and the docs have a short
"Supported isolated host" section saying the same, including that a
separate-uid design would need its own spec. Both issues close.

## Affected users and systems

Operators running trusted-plan (Task Contract v2) builds. Files:
`trusted_contract.py` (comment only) and `docs/docs/task-contract.md`.
No runtime behaviour changes.

## Constraints

- Do not change `host_isolated()` logic.
- Keep `test_pid_namespaced_sandbox_host_is_not_isolated` as is; it pins the decision.

## Open questions

None. Owner decision (B): keep kubejob + durable store only.
