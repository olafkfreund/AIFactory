---
status: approved
issue: 1704
intent: intent/2026-10-17-1704-kubejob-build-ownership.md
---

# Spec: one owner per build, visible from every replica

Covers #1704, #1705, #1714. Three options were offered; **the user chose Option A**
(see "Decisions" below). Options B and C are kept for the record, marked not chosen.
Line numbers are against `origin/dev` at `08e22641`.

## Decisions (user, 2026-10-17)

**Option A is chosen.** The `job_states` row is the only owner of a build;
Kubernetes is evidence only, never an order.

- **#1704:** Start and plan approval return 409 while `is_running_anywhere` is
  true, and also when the store read fails. They never call `stop_task` on a
  build; they may still stop a local spec-creation process.
- **#1705:** `worker_ref` records `{kind: subprocess, pod: HOSTNAME, boot: <boot id>}`.
  `reconcile_on_startup` fails rows from this pod's earlier boot. The reaper
  fails another pod's row only when `read_namespaced_pod` returns 404.
- **#1714:** the label-based Job sweep with the 10-minute grace, deleting one Job
  at a time and never with `delete_collection`. It ships in **dry-run by
  default** (report only) behind an env switch that follows `stale_reaper`'s
  `REAPER_DRY_RUN` (anything but an explicit `false` reports). Forward the
  switch into build Jobs only if it is needed there.
- **Credential:** dispatch catches `BaseException` and releases the credential
  only when no Job can exist.

Answers to the intent's open questions: (1) 409 instead of restarting; (2) the
pod-404 rule is allowed; (3) dry-run first. No migration and no new RBAC. If
list or read RBAC is missing, report it rather than adding it.

## Facts every option builds on

- The durable row (`job_states`, `database/models.py:1191-1195`) is already
  the cross-replica lock: `admit` (`services/job_state_store.py:202-282`)
  raises "already running" for any active row, under an advisory lock and
  `FOR UPDATE`. A second replica cannot double-start a task whose row is live.
- For `k8s-job` rows, the Job object is already the ground truth:
  `reap_vanished_jobs` (`services/build_backend.py:1476`) reads Job status and
  checks the `factory.io/job-id` label before writing a verdict
  (`build_backend.py:1371-1410`). Any replica can do this safely; it never
  kills a Job, only records what the kubelet reports.
- For `subprocess` rows there is no external truth: only the pod that spawned
  the process can see or signal it. `mark_running` (`job_state_store.py:283-302`)
  records no owner.
- Spec creation (`services/agent_spec_creation.py:80`) is always a pod-local
  subprocess with no durable row. The "stale spec creation process" cleanup in
  Start (`routes/execution.py:699-715`) and approval
  (`routes/plan_approval.py:170-184`) exists for that process only.
- The token pool (`services/claude_token_pool.py:74`) is in-memory per pod and
  falls back to sharing when every credential is busy (`:116-128`), so a held
  credential lowers distinctness but never blocks a build.

## Option A (CHOSEN): the row is the owner; Kubernetes is evidence, never an order

Smallest change that closes all three holes, no migration, no new RBAC.

**A1 (#1704): Start and approval never stop a build.**
Replace the `is_running` + `stop_task` block in both routes with one shared
helper (in `agent_service.py`, beside `stop_task`):

- if `await is_running_anywhere(task_id)` and the task is NOT a local
  spec-creation process: raise 409 "Task is running (possibly on another
  replica)". Never call `stop_task` / `_stop_kubejob_build` from these routes.
- if the only thing running is a local spec-creation process
  (`task_id in running_tasks` and not a kubejob/admitted build): terminate it
  exactly as today (`stop_task` on the local process).
Store read errors already read as "running" (`agent_kubejob.py:1017-1022`), so
the route fails closed with 409.

**A2 (#1705): subprocess rows carry their owner, and only provable orphans close.**
- `mark_running` stamps `worker_ref={"kind": "subprocess", "pod": <HOSTNAME>,
  "boot": <uuid generated once per process>}` (JSON column, no migration).
- `reconcile_on_startup` (`agent_service.py:1517`) marks terminal
  (`failed`, error "control plane restarted under the build") every
  `subprocess` row whose `pod` equals this pod's HOSTNAME and whose `boot`
  differs from this process's. A container restart keeps the pod name, and the
  old process is dead by construction (one server process per container).
- On the kubejob reaper tick (any replica), a `subprocess` row whose `pod` is
  another pod is marked terminal only if `read_namespaced_pod` returns 404 for
  that name (Pods `get` is granted). Any other error, or a row with no `pod`
  stamp (rows written before this change), is left alone. Legacy rows are
  cleared by Recover with `force=true`, as today.
- Then `is_running_anywhere` counts `subprocess` rows too (add it to
  `_LIVE_KINDS`, `agent_kubejob.py:32`) and reads the store whenever the store
  is enabled, not only on the kubejob backend (`agent_kubejob.py:1009-1013`).

**A3 (#1714): sweep Jobs nobody owns; hold the credential until the Job is gone.**
- Sweep (in `reap_vanished_jobs`' loop, after the row pass): list Jobs in the
  build namespace with selector `app=aifactory,factory.io/kind=task`
  (`core/job_dispatch.py:298-308`). Delete one (single `delete_namespaced_job`,
  `propagation_policy="Background"`; no `delete_collection`) only when ALL hold:
  its `creationTimestamp` is older than a grace (10 min, longer than the
  create-then-`set_worker_ref` window); no row in `ACTIVE_STATES` has this
  `job_name` in its `worker_ref`; no `pending` row reconstructs to this name
  (`_reconstructed_ref`, the #1606 path); and the store read for this pass
  succeeded. Any read error skips the whole sweep for that tick.
- Credential: in `_dispatch_build_job` (`agent_kubejob.py:441-460`) catch
  `BaseException`, not `Exception`. Release the credential only when dispatch
  proves no Job exists (create failed, or the rollback delete succeeded). The
  backend signals the double failure with a dedicated exception (subclass of
  the current error, carrying namespace and job name); on it the caller keeps
  the credential checked out and records `(namespace, job_name)` in a
  pod-local `_held_credentials` map. This pod's reaper tick releases each held
  credential once `read_namespaced_job` returns 404 (deleted by the sweep or
  by its deadline). A pod restart drops the pool anyway, so there is nothing
  to persist.

What breaks: Start/approve with an approved plan over a live build now 409s
instead of stopping and restarting (only ever reachable on the replica that
dispatched it). Legacy unstamped `subprocess` rows stay stuck until a forced
Recover (same as today). Job names carry only the last 20 characters of the id
(`_short`), so the sweep matches rows by recorded `job_name`, never by id.

Migration: none. New stamps appear as builds start; old rows behave as today.

Test plan (`apps/web-server/tests/`, existing fakes for the store and batch API):
- Start and approve: live row on another replica → 409, `stop_task` not
  called; local spec process only → terminated as today; store read raises →
  409.
- `reconcile_on_startup`: same pod + old boot → failed; same pod + same boot,
  other pod, unstamped → untouched.
- Reaper: other pod 404 → failed; pod exists / 500 / timeout → untouched.
- `is_running_anywhere`: running `subprocess` row on subprocess backend → True.
- Sweep: orphan Job past grace → deleted once; Job younger than grace, Job
  matching a running row, Job matching a reconstructed pending row, store
  error → not deleted; never calls `delete_collection`.
- Dispatch: create OK + ref fails + delete fails → credential still held,
  released after the Job 404s; `CancelledError` mid-dispatch → credential
  released when no Job was created.

## Option B (not chosen): Kubernetes objects as the source of truth

Label every Job with `factory.io/owner-pod`, and answer "running anywhere" by
listing Jobs (and, for in-pod builds, by listing the owning pods). Drop the
row as the liveness authority.

What breaks: every Status/Start/Stop/Recover call hits the API server (latency,
and an API outage makes everything read "running"); subprocess builds have no
Kubernetes object of their own, so the in-memory/dev and non-Kubernetes paths
lose cross-replica answers entirely; the advisory-lock admission in
`admit` would have to be rebuilt around a non-transactional source. Migration:
relabel in-flight Jobs or wait for them to drain. Test plan: large; every route
test needs a fake API server.

Rejected as the main design. Its useful part, listing Jobs by label to find
orphans, is A3.

## Option C (not chosen): DB leases with heartbeats for every row

Add `owner` and `lease_expires_at` columns (Alembic migration). Each replica
renews the lease of every build it owns every 15 s; a row whose lease expired
is an orphan; any replica may adopt a `k8s-job` row (take over log streaming
and the credential) or fail a `subprocess` row.

What breaks: a live owner that stalls (long GC, DB blip, event-loop block)
loses its lease and another replica fails its row while the subprocess keeps
running and writing; `admit` then lets a second build start beside it — the
exact harm the constraint forbids, so the TTL must be generous and the
Kubernetes check of A2 is still needed to be safe. Clock skew between replicas
and the DB matters. Adoption of the pooled credential is impossible (the pool
is per pod). Migration: new columns, backfill as "unknown owner", renew loop.
Test plan: A's plus lease expiry, renewal under load, and clock-skew cases.

Rejected for now: more moving parts than A and still needs A's pod check to
be safe. Worth revisiting if deployments outside Kubernetes ever run more
than one replica (A2's pod check has no meaning there).

## Options at a glance

| | A: row owner + k8s evidence (chosen) | B: k8s as truth (not chosen) | C: DB leases (not chosen) |
|---|---|---|---|
| Closes #1704 / #1705 / #1714 | yes / yes (stamped rows) / yes | yes / partly / yes | yes / yes / needs A3 |
| Migration | none | relabel Jobs | Alembic + backfill |
| New RBAC | none | pods list on hot path | none |
| Can kill a live build of another replica | no (404 proof only) | no | yes, on a missed heartbeat |
| Size | ~4 small edits + tests | large | large |

## Risks

- A2's pod check trusts HOSTNAME = pod name, which holds for Kubernetes pods
  but not for `hostNetwork` pods or a custom hostname. Mitigated: such rows
  only fail to be reaped (stay stuck), never get reaped wrongly, unless two
  pods share a hostname; the chart sets neither.
- A3's sweep runs on every replica; deletes are idempotent (404 is success).
- A1 changes a user-visible flow (Start over a live build). The UI shows the
  409 detail.

## Verification

- `cd apps/web-server && pytest tests/ -k "kubejob or job_state or plan_approval or start_task"`
- `ruff check` and `ruff format --check` on touched files; mypy per repo config.
- Read-only on a multi-replica cluster: `kubectl get jobs -l app=aifactory,factory.io/kind=task`
  before and after a sweep tick shows only Jobs with a running row.
