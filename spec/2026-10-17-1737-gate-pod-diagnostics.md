---
status: approved
issue: 1737
intent: intent/2026-10-17-1737-gate-pod-diagnostics.md
---

# Spec: name the pod phase when a gate Job fails or times out

## Design

All in `apps/backend/core/kube_sandbox.py`.

1. New pure helper `_pod_state_summary(pod) -> str`, next to
   `_exit_code_from_pod` (`:330`). It reads `pod.status.phase` and, for
   `init_container_statuses` then `container_statuses`, each container's
   `state.waiting` or `state.terminated` reason (message appended, cut to
   200 chars). Example: `pod Pending; unpack-workspace: waiting
   PodInitializing; runner: waiting ErrImagePull (back-off pulling ...)`.
   Containers that are running, or have no reason, are skipped. Any
   exception: `logger.warning` and return `""`.
2. `_run_async` (`:441-466`) already lists the Job's pods BEFORE the
   `finally` deletes the Job. When the result is not a success
   (`succeeded` false after `_exit_code_from_pod`), append
   ` [<summary>]` to the output text when the summary is non-empty. When no
   pod is left, append ` [no pod found]`. The `succeeded`/`exit_code` values
   are computed before and are never touched by this step.

No new API call: the pod object from the existing `list_namespaced_pod`
holds the statuses. No RBAC change.

## Alternatives rejected

- A separate `read_namespaced_pod` call: the listed pod already has the
  same status; one more call adds a failure path for no data.
- Polling pod state on every loop iteration (to catch a pod the Job
  controller already deleted on `DeadlineExceeded`): more calls and code;
  start with the cheap read and see what the next timeout shows.
- Delaying the Job delete: keeps pods around longer and changes cleanup.

## Risks

- On `DeadlineExceeded` the Job controller kills the pod; if it is already
  gone when listed, the output says `no pod found` (still more than today).
- Diagnostics code raising: guarded by try/except, gate result unchanged.

## Verification

- Unit tests in `tests/test_kube_sandbox.py`: the helper on waiting,
  terminated, init-container and broken pod objects; `_run_async` with a
  failed Job and a pending pod includes `ErrImagePull` in the output and
  keeps `ok=False`; a succeeded Job output is unchanged.
- `ruff check`, `ruff format --check` on the touched files.
