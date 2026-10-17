---
status: approved
issue: 1737
author: olafkfreund
---

# Intent: name the pod phase when a gate Job fails or times out

## Problem

The Swift gate hits its 600s deadline in the build Job (#1737). The
diagnosis (issue comment) suspects a cold pull of the multi-GB gate image,
but there is never evidence: `KubeJobSandbox._run_async`
(`apps/backend/core/kube_sandbox.py:402-474`) deletes the Job in its
`finally` as soon as the result is back, so the pod and its state are gone
before anyone can look. The gate output only says `[job DeadlineExceeded]`
plus whatever log there was.

## Proposed outcome

When a gate Job does not succeed, its output also names the pod phase and
each container's waiting/terminated reason (for example `ContainerCreating`,
`ErrImagePull`, `OOMKilled`), read before the Job is deleted. The next
timeout says which phase it died in, without kubectl.

## Affected users and systems

- `apps/backend/core/kube_sandbox.py` (nixjob/kubejob gate backend).
- Operators reading gate output; no change to the gate result.

## Constraints

- Read-only, through the existing kube client and the pod object the code
  already lists. No new RBAC.
- Diagnostics must never change the gate result: any failure is logged and
  skipped.
- Root cause stays an operator decision: the PR uses `Refs #1737`.

## Open questions

None.
