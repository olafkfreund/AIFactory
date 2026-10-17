---
status: draft
issue: 1704
author: olafkfreund
---

# Intent: one owner per build, visible from every replica

Covers #1704, #1705 and #1714 (follow-ups from #1669 and #1677). All three are
open on `origin/dev` (checked 2026-10-17 at `08e22641`); none is already fixed.

## Problem

AIFactory decides "is this build running, and who may stop it" in three
different ways, and they disagree once there is more than one control-plane
replica or a pod restarts.

1. **Start and plan approval look only at this pod (#1704).**
   `routes/execution.py:699` and `routes/plan_approval.py:170` call the
   pod-local `AgentService.is_running` (`services/agent_service.py:1552`), and on
   a hit call `stop_task` (`agent_service.py:1343`) to clean up a "stale spec
   creation process". `is_running` is true for a kubejob build this replica
   dispatched (`_active_kubejob_task_ids`), so the cleanup can reach
   `_stop_kubejob_build` (`services/agent_kubejob.py:1113`) and delete a live
   Job. On another replica the same request sees nothing and relies on
   `JobStateStore.admit` (`services/job_state_store.py:226`) raising
   "already running" for the 409. Status, Stop and Recover already read the
   shared store through `is_running_anywhere` (`agent_kubejob.py:1002`, #1669);
   switching Start and approval to it as-is would make every replica stop a
   live build it does not own.

2. **In-pod (subprocess) builds have no owner on record (#1705).**
   `mark_running` (`job_state_store.py:283`) stamps
   `worker_ref={"kind": "subprocess"}` with no pod or process identity. When
   the pod dies, nothing ever marks that row terminal:
   `reconcile_on_startup` (`agent_service.py:1517`) only counts rows, and the
   kubejob reaper (`build_backend.py:1476`) only reads `k8s-job` rows. The row
   keeps a concurrency slot and makes `admit` refuse every restart of that task
   with 409. Because of this, `is_running_anywhere` deliberately ignores
   `subprocess` rows (`_LIVE_KINDS`, `agent_kubejob.py:32`) and reads the
   store only on the kubejob backend (`agent_kubejob.py:1009-1013`), so a live
   in-pod build on replica A is invisible to replica B.

3. **A Job can outlive every record of it (#1714).**
   Dispatch creates the Job, then writes the ref (`build_backend.py:1270-1290`).
   If both the ref write and the rollback delete fail, the Job runs with no
   row pointing at it, bounded only by the 6 h deadline
   (`build_backend.py:179`). Nothing lists Jobs to find it. Separately, the
   pooled Claude credential is released as soon as dispatch raises
   (`agent_kubejob.py:457-460`) even when the Job may still be running on it,
   and that handler catches only `Exception`, so a `CancelledError` during
   dispatch leaks the credential instead.

## Proposed outcome

- Every replica gives the same answer to "is this task's build running", for
  both backends, and that answer never stays "running" forever after its pod
  or Job is gone.
- Start and plan approval never stop a build. They clean up only a stale
  local spec-creation process; a live build anywhere gives a clear 409.
- A Job with no running row pointing at it is found and deleted, and the
  credential it was given is not handed to another build until the Job is
  gone.
- No replica ever deletes a Job or fails a row for a build that is still
  live under another replica.

## Affected users and systems

- `apps/web-server/server/routes/execution.py`, `routes/plan_approval.py`
- `services/agent_service.py`, `services/agent_kubejob.py`,
  `services/build_backend.py`, `services/job_state_store.py`
- Deployments with `replicaCount > 1` and `AIFACTORY_BUILD_BACKEND=kubejob`
  (the chart defaults to 1 replica, and forces 1 when rmux is on:
  `charts/aifactory/templates/deployment.yaml:12`); single-replica deployments
  after a pod restart (orphaned subprocess rows).
- Operators: Start/approve behaviour changes from "stop and restart" to 409
  when a build is live.

## Constraints

- Must never kill, delete or fail a live build owned by another replica. Any
  doubt (store read error, Kubernetes API error, unknown row shape) reads as
  "live, leave it alone", matching #1551.
- Must not need new RBAC: the sandbox Role grants Jobs
  `create/get/list/watch/delete` and Pods `get/list/watch`
  (`charts/aifactory/templates/rbac/job-sandbox-rbac.yaml:35-40`); no
  `deletecollection`.
- Must keep working on the subprocess backend and with the in-memory store
  (single-pod dev).
- No schema migration unless the chosen option needs one.

## Open questions

1. When a plan is approved while a build for the task is live, should Start
   and approval refuse with 409 (proposed) instead of stopping the build and
   starting again?
2. May a replica decide that another pod's in-pod build is dead by asking the
   Kubernetes API whether that pod still exists, or should orphaned subprocess
   rows only ever be closed by their own pod on restart (or by a lease)?
3. Should the Job sweep ship in delete mode, or report-only first like
   `stale_reaper` (`REAPER_DRY_RUN`)?
