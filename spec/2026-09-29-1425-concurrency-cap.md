---
status: draft
issue: 1425
intent: intent/2026-09-29-1425-concurrency-cap.md
---

# Spec: bound the disk per build with a limit, then raise the cap to 12

## What the measurement found

The intent refused to name a number without one. A live build Job was running
during the investigation, so the figure is measured rather than modelled —
`kubectl get --raw /api/v1/nodes/k3d-factory-server-0/proxy/stats/summary` at
`2026-09-29T09:51:08Z`, pod `factory-aifactory-shared-core-remediat-8kzqf`,
28 minutes in:

| component | MB | |
| --------- | -- | - |
| `clis` emptyDir | **790.1** | the `install-clis` initContainer (see "Interaction" below) |
| container rootfs | 50.7 | incl. `/tmp/factory-nix-store` — **empty in this pod** |
| `work` emptyDir | 5.7 | a docs/specs repo, no flake |
| agent home dirs | 4.9 | `cc-claude`/`cc-codex`/`cc-gemini`/`cc-config` |
| logs | 1.0 | 28 minutes of agent log |
| **pod total** | **852.4** | the rows sum to the kubelet's own figure exactly |

Three things this corrects in the intent:

1. **The Nix store is not in the build pod.** `AIFACTORY_SANDBOX_GATES=true` +
   `AIFACTORY_SANDBOX_BACKEND=nixjob` mean gates dispatch a *separate*
   `KubeJobSandbox` Job (`gate_runner._nix_kube_runner`, `kube_sandbox.py:118`).
   `/tmp/factory-nix-store` in the build pod stays near zero — which the 50.7 MB
   rootfs confirms. A slot is therefore *build pod + its overlapping gate pod*.

2. **`/work` is small, and measured.** `_tar_workspace` (`artifact_store.py:297-333`)
   has no exclusions, so `.git`, `node_modules` and `.venv` all pack. Across 234
   historical packed workspaces in minio (2026-07-17 → today): p50 0.3 MB,
   p75 4.6 MB, p90 6.4 MB, **p95 116.1 MB**, max 116.6 MB. The largest real
   workspace decodes to 130.4 MB uncompressed (93.0 MB of it `.git`).

3. **The node advertises five times the disk it has.** Allocatable
   `ephemeral-storage` is 933.7 GB; actually available is **185.4 GB** of
   982.8 GB capacity. The other ~747 GB is the workstation's own data — this
   node's filesystem *is* the host root. Verified independently: `/nix` 316 GB,
   `/home` 203 GB, `/var` 121 GB (92 GB of it host containerd), `/mnt` 42 GB.

Per-slot peak, with #1608 closed (the gate image is already warm for
swift/kotlin, so the chroot store only takes a per-repo delta):

```
build pod = 790 (clis) + 130 (work p95) + 51 (rootfs) + 11 (homes, logs) ≈ 982 MB
          + in-build /work growth (node_modules/.venv/artifacts)   +0…1000 MB  ← unmeasured
gate pod  = 130 (repo emptyDir) + a few hundred MB (store delta)   ≈ 430 MB
                                                     peak ≈ 1.5 GB typical, 2.4 GB worst
```

## Design

### 1. A limit, not a request — and this is the crux

`job_dispatch.py:577` currently sets only `limits`:

```python
"resources": {"limits": {"cpu": spec.cpu_limit, "memory": spec.mem_limit}},
```

Kubernetes derives equal `requests` from it, which the live pod confirms.
`"ephemeral-storage": "4Gi"` joins that dict — one line, request follows.

**A request would be actively harmful here.** The scheduler reasons against
allocatable, which is 933.7 GB of fiction; at 4Gi it would cheerfully admit
~200 concurrent builds against a pool that does not exist. The *limit* is what
binds, because the kubelet enforces it per pod: a runaway build is evicted by
itself, instead of the node crossing `evictionHard nodefs.available: 5%` and
evicting arbitrary pods — postgres and minio among them.

4Gi covers the 2.4 GB worst case with margin for the one unmeasured term.

**No emptyDir `sizeLimit`** (`:542`). emptyDir usage already counts toward the
pod's ephemeral-storage limit, so the pod limit covers `/work` and `/clis` both;
a per-volume limit would only produce a more specific eviction message.

**Optionally 2Gi on the gate pod** (`kube_sandbox.py:118`), which is the other
half of a slot.

**No invariant trips.** `assert_job_policy` (`:429-505`) checks kind, DNS-1123
name, `backoffLimit==0` and pod labels; `grep -c resources` over that range is
0. The self-tests at `:789`/`:837+` do not touch resources. Only
`tests/test_kube_sandbox.py:30` asserts a resources value (`memory == "2Gi"`),
unaffected.

### 2. The cap: 12

```
usable = available − eviction reserve − churn reserve
       = 179 GB   − (5% × 982.8 = 49.1) − 30 (2× the observed swing)  =  99.9 GB
cap    = 99.9 / 4.29 GB (4 GiB)  =  23
```

Nothing else binds: CPU 128/2 = 64, memory 261.6 GB / 4Gi = 65, pods
(110−38)/2 = 36.

**Ship 12, not 23** — half the disk-derived figure, halved because in-build
`/work` growth (a task that runs `npm install` or builds artifacts) is the one
term nobody has measured. 12 is still a 2.4× increase over today's 5, and the
arithmetic for 23 is recorded so raising it later is a one-line change with a
known basis rather than another guess.

The churn reserve is not decoration: six samples 60 s apart gave
179.2 / 193.5 / 193.5 / 191.1 / 185.5 / 185.6 GB — ±15 GB in six minutes, all of
it host-side, since every large consumer on that disk belongs to the
workstation.

Set in gitops on the aifactory Deployment; `config.py:220` stays at 5 as the
default for off-cluster installs, which have no reason to inherit this node's
arithmetic.

### 3. Correcting the intent on disk pressure

The intent said the kubelet was logging `FreeDiskSpaceFailed`. That was true on
2026-09-24 and is not true now: `kubectl get events -A --field-selector
reason=FreeDiskSpaceFailed` returns nothing, `DiskPressure` has been `False`
since 2026-09-23T22:41:35Z, and at ~76% the node never reaches
`imageGCHighThresholdPercent: 85`, so GC does not fire. Images are 4.1% of used
space (30.7 GB), so reclaiming them would not help either. The pressure was real
and is not currently present — which is an argument for the churn reserve, not
against the change.

## Interaction with #1621 — the bigger lever

`/clis` is 790 MB of the 982 MB build-pod floor, and #1621 establishes it is
pure duplication: the same three CLIs are already baked at pinned versions
(`Dockerfile:275-282`), the initContainer reinstalls them **unpinned** and
shadows them, and its justification comment is stale.

If #1621 lands, the build pod floor falls to ~192 MB and per-slot peak to well
under 1 GB, at which point 4Gi is generous and the cap could go considerably
higher.

**This spec does not depend on #1621 and does not wait for it.** 4Gi is correct
either way — it is an eviction ceiling, not an allocation — and 12 is safe with
`/clis` still present. Sequencing them would hold a 2.4× improvement behind an
unrelated change.

## Alternatives rejected

**An `ephemeral-storage` request.** The scheduler evaluates it against a
933.7 GB allocatable that overstates reality 5×. Worse than nothing: it would
license the very overcommit this is meant to prevent.

**Raise to 20 as the issue asks.** 20 × 4.29 GB = 85.8 GB against a 99.9 GB
usable floor that swings ±15 GB — no margin for the unmeasured `/work` growth.
Defensible only after #1621.

**Cap 1, as a reviewer proposed.** Made sense when the belief was that image GC
was failing at 85% with 131 GB free. It is not, there is 185 GB, and 1 would be
a regression from today's 5.

**An emptyDir `sizeLimit` instead of a pod limit.** Covers `/work` but not the
rootfs or `/clis`; the pod limit covers all three.

**Leave the cap at the code default and set nothing.** Keeps an operational
parameter invisible and undocumented, which is how it came to be unknown.

## Risks

- **Eviction of a legitimate build that exceeds 4Gi.** The real risk, and the
  reason for 4Gi rather than 2Gi. A task that vendors a large `node_modules`
  into `/work` could hit it. Mitigated by margin; the failure is one build
  evicted with a clear reason, versus the current failure of an unbounded pod
  taking the namespace with it.
- **The unmeasured term.** In-build `/work` growth is why the cap is halved. If
  a build is later observed above 4Gi, the value moves — it is one line.
- **Host churn eats the headroom.** The node's disk is shared with the
  workstation and moved ±15 GB in six minutes. Reserved for; not eliminable from
  inside the cluster.
- **12 is still a guess about concurrency, not about disk.** Observed
  concurrency is ~1; nothing proves 12 builds behave like 12 × 1 build.
  Unmeasurable without generating the load.
- **Applying restarts the pod.** Cluster-affecting; needs a window.

## Verification

1. **The limit is present** in a dispatched Job's manifest and the pod reports
   `ephemeral-storage` under both limits and requests.
2. **`assert_job_policy` still passes**, and the policy self-tests are unchanged.
3. **A pod exceeding the limit is evicted** rather than the node — provable in a
   throwaway pod that writes past 4Gi, without a real build.
4. **The cap is read from the environment**: with `APP_MAX_CONCURRENT_TASKS=12`
   set, `admit()` grants a 6th concurrent slot, which it refuses today.
5. **The default is unchanged** for a deployment that sets nothing —
   `config.py:220` still yields 5.
6. **Existing suite green**, and cq-ratchet "0 regressed" on both halves.
7. **Post-apply, on the cluster:** a build runs to completion with the limit in
   place, and `stats/summary` shows its `ephemeral-storage.usedBytes` under 4Gi.
   This is the only item that needs the cluster, and it needs one real build.
8. **Not verified:** that 12 concurrent builds are safe. Nothing short of
   generating that load proves it, and the margin exists precisely because it is
   unproven.
