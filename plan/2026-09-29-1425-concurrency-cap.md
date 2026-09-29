---
status: draft
issue: 1425
spec: spec/2026-09-29-1425-concurrency-cap.md
---

# Plan: an ephemeral-storage limit on the Job, then the cap at 12

Self-contained summary of the approved decisions.

## Approved decisions (carried from the spec)

- **Declare the disk before raising the cap.** Raising an admission limit while
  the scheduler is blind to the resource that actually binds is how the namespace
  gets evicted. Order matters more than the number.
- **A limit, not a request.** Node allocatable `ephemeral-storage` is 933.7 GB;
  real headroom is **185.4 GB**, because this node's filesystem *is* the
  workstation root (`/nix` 316 GB, `/home` 203 GB, `/var` 121 GB). A request is
  evaluated against that 5× fiction and would admit ~200 builds against a pool
  that does not exist. A limit is kubelet-enforced per pod: the offending build
  is evicted by itself, instead of the node crossing
  `evictionHard nodefs.available: 5%` and evicting arbitrary pods — postgres and
  minio among them.
- **`ephemeral-storage: 4Gi`** in the same `limits` dict; Kubernetes derives an
  equal request. Covers the 2.4 GB worst case with margin for the one unmeasured
  term.
- **No emptyDir `sizeLimit`.** emptyDir usage already counts toward the pod's
  ephemeral-storage limit, so the pod limit covers `/work` and the rootfs both; a
  per-volume limit would only make the eviction message more specific.
- **Cap 12**, from `(179 − 49.1 eviction reserve − 30 churn reserve) / 4.29 GB =
  23`, halved because in-build `/work` growth is unmeasured. Still 2.4× today's 5.
  Set in gitops; `config.py:220` stays at 5 for off-cluster installs.
- **Optionally 2Gi on the gate pod** (`kube_sandbox.py:118`), the other half of a
  slot.
- The measurement is from a **live build Job**, decomposed by the kubelet:
  `/clis` 790.1 MB, rootfs 50.7 MB, `/work` 5.7 MB, homes 4.9 MB, logs 1.0 MB,
  pod total 852.4 MB — the rows sum to the kubelet's own figure.

## Verified facts this plan relies on

- `core/job_dispatch.py:577` — `"resources": {"limits": {"cpu": spec.cpu_limit,
  "memory": spec.mem_limit}}`. Only `limits` is set; the live pod shows
  Kubernetes derived equal requests.
- `core/job_dispatch.py:168-169` — `cpu_limit: str = "2"`, `mem_limit: str = "4Gi"`
  on `JobSpec`. A third field follows the same shape.
- `core/kube_sandbox.py:118` — the gate pod's `"resources"` dict, same shape.
- `assert_job_policy` (`:429-505`) checks kind, DNS-1123 name, `backoffLimit==0`
  and pod labels. `grep -c resources` over that range is **0**, and the
  self-tests at `:789`/`:837+` do not touch resources, so no invariant trips.
- `tests/test_kube_sandbox.py:30` is the only test asserting a resources value
  (`memory == "2Gi"`) — unaffected by adding a key.
- `apps/web-server/server/config.py:220` — `MAX_CONCURRENT_TASKS: int = 5`, the
  effective value today because nothing sets the env anywhere.
- Node state 2026-09-29: capacity 982.8 GB, available 185.4 GB (75% used),
  imageFs 30.7 GB (4.1% of used), `DiskPressure False` since 2026-09-23,
  `imageGCHighThresholdPercent: 85` so GC never fires, and **no**
  `FreeDiskSpaceFailed` events — correcting the intent, which was written when
  there were.
- Headroom swings ±15 GB in six minutes (six samples 60 s apart), all host-side.

## Steps

1. `apps/backend/core/job_dispatch.py:168-169` — add
   `ephemeral_storage_limit: str = "4Gi"` to `JobSpec`, beside the cpu and memory
   limits, with a comment recording why it is a limit and not a request. → verify
   by a unit test asserting the default.

2. `apps/backend/core/job_dispatch.py:577` — add
   `"ephemeral-storage": spec.ephemeral_storage_limit` to the `limits` dict.
   → verify by a test that a built manifest carries it, and that Kubernetes-style
   equal requests are not hand-written (we let k8s derive them, as it does for
   cpu/memory today).

3. `apps/backend/core/kube_sandbox.py:118` — add an `ephemeral-storage` limit to
   the gate pod's resources, defaulting to `2Gi`, plumbed the same way `cpus` and
   `memory` already are. → verify by a test on the gate manifest.

4. `tests/` — the spec's verification list: the limit is present on both
   manifests; `assert_job_policy` still passes; `test_kube_sandbox.py:30`
   unchanged. → verify by the commands below.

5. Commit, then both halves of cq-ratchet (it diffs committed history). → verify
   by "0 regressed" from each.

6. Open the PR against `dev` linking all three artifacts. → verify by the PR body
   and `Closes #1425`.

7. **gitops, separately and only with explicit go-ahead** (it restarts the pod):
   `APP_MAX_CONCURRENT_TASKS: "12"` on the aifactory Deployment, with the
   arithmetic in a comment beside it so the next person raising it has the basis
   rather than a bare number. → verify by the ordering note below.

## Ordering

**The limit must be deployed before the cap is raised.** Raising the cap first
puts more builds on a node the scheduler still cannot protect, which is precisely
the failure this issue is meant to prevent. So: merge and deploy the code, then
apply the gitops value. Unlike #1607, there is no inert-first option — the gitops
value has effect the moment it lands.

## Tests

```bash
apps/backend/.venv/bin/pytest tests/ -k "job_dispatch or kube_sandbox or job_policy" -v
apps/backend/.venv/bin/pytest tests/ -m "not slow" -q
```

Expected: new resource assertions pass; the policy self-tests and
`test_kube_sandbox.py` pass unchanged; cq-ratchet "0 regressed" on both halves.

Ratchet note: it counts TID252, untyped defs, PLC0415 and DTZ006. Type the new
field and any test helper.

## Post-deploy verification (needs the cluster, one real build)

- a dispatched Job carries `ephemeral-storage` under limits **and** requests
- the build completes, and `stats/summary` shows its
  `ephemeral-storage.usedBytes` under 4Gi
- after the gitops change, `admit()` grants a 6th concurrent slot

A pod exceeding the limit being evicted rather than the node can be proven
without a real build, in a throwaway pod that writes past 4Gi.

## Rollback

`git revert <sha>` for the code; delete the gitops line for the cap. Reverting
the limit removes a ceiling — it cannot make a previously-schedulable build
unschedulable, so the revert is safe in either order. **Revert the cap first if
both are being undone**, so the node is not left at 12 concurrent builds with no
disk ceiling.

## Out of scope

- A per-tenant cap. `MAX_CONCURRENT_TASKS` is global, so on a multi-tenant
  control plane one tenant can consume every slot. Recorded in the intent as
  worth its own issue rather than silently preserved.
- Excluding `.git`/`node_modules` from the packed workspace
  (`artifact_store.py:297-333` has no exclusions). Would shrink `/work`
  materially — p95 is 116 MB, 93 MB of it `.git` — but it changes what a build
  receives, which is a different risk from a disk ceiling.
- Proving 12 concurrent builds are safe. Nothing short of generating that load
  proves it; the margin exists because it is unproven.
