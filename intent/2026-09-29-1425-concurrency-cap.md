---
status: draft
issue: 1425
author: Olaf Krasicki-Freund
---

# Intent: let more builds run at once, without letting them fill the node

## Problem

`APP_MAX_CONCURRENT_TASKS` bounds how many builds run concurrently. The issue
asks to raise it from 5 to 20. Three things about that framing are wrong, and one
prerequisite has only just landed.

**There is no value to edit.** The key is not set anywhere — not in this repo, not
in `factory-gitops` (checked across all remote refs; the only mentions are prose
in `apps/keda/`), and not in the live pod's env. The effective value is the code
default, `apps/web-server/server/config.py:220 MAX_CONCURRENT_TASKS = 5`. This
would be a first-time addition.

**The stated ceiling is not the real one.** The issue names RWO node-pinning.
That is already gone: `AIFACTORY_PACK_WORKSPACE=true` and
`AIFACTORY_PACKED_NIX_IN_IMAGE=true` are live, the packed path sets
`data_pvc=None`, and `/work` is an emptyDir — and the cluster is a single node
regardless. CPU and memory are not the ceiling either: the node allocates 128 CPU
and 251 GiB, currently at 3% and 4% requested, while each build Job asks
`cpu=2 / mem=4Gi` (`core/job_dispatch.py:168-171`). Twenty concurrent builds
would want 40 CPU and 80 GiB, which fits comfortably.

**Disk is the ceiling, and nothing bounds it.** `/work` is an emptyDir with no
`sizeLimit` (`job_dispatch.py:542`) and the writable Nix store is
`/tmp/factory-nix-store` on the container layer (`:224`); both land on the node
overlay. Measured on 2026-09-29: 982.8G capacity, **199.7G available (75% used)**
— better than the 131G/85% of last week, but the kubelet was logging
`FreeDiskSpaceFailed … only found 0 bytes eligible to free` then, so the headroom
moves. Critically, **no Job declares `ephemeral-storage`**: the node reports
`ephemeral-storage 0 (0%)` requested. The scheduler therefore cannot refuse a
build that would fill the disk, and a full node evicts the namespace.

**The prerequisite is now met.** The issue's own acceptance criterion says to
"watch for stranded `running` rows after the change". Until #1606 that was
toothless — a stranded row consumed a slot permanently and nothing reaped it, so
raising the cap would have given the leak more room to hide in. #1606 is merged,
so the slot accounting can now be trusted.

Finally, the cap is not currently binding on anything: `job_states` holds 21 rows
lifetime, a maximum of 8 in one day, with durations of 0.8–2.9 seconds. Observed
concurrency is effectively 1.

## Proposed outcome

More builds can run at once, and the node cannot be filled by them.

Observable:

- The concurrency cap is a declared, reviewable value rather than an undocumented
  code default.
- A build Job declares what disk it needs, so the scheduler refuses one the node
  cannot host instead of discovering it by eviction.
- The cap is set from a measured figure, and the reasoning is recorded next to it.
- Raising the cap later is a one-line change with a known basis, not a guess.

## Affected users and systems

- `factory-gitops apps/aifactory/manifests` — where the cap becomes explicit.
  **Applying it restarts the pod**, so it needs a deliberate window.
- `apps/backend/core/job_dispatch.py` — the Job resource spec, if
  `ephemeral-storage` is declared there.
- The `factory` node: builds that previously scheduled freely would become
  subject to a disk request.
- Anyone whose tasks queue behind the current cap of 5 — nobody today, on the
  evidence, but that is the point of raising it.

## Constraints

- **Declare the disk before raising the cap.** Raising an admission limit while
  the scheduler is blind to the resource that actually binds is how the namespace
  gets evicted. Order matters more than the number.
- The number must come from a measurement of real per-build ephemeral use, not
  from the issue's 20, not from my 10, and not from a reviewer's 1. All three
  were guesses.
- Must not make builds unschedulable: an `ephemeral-storage` request that exceeds
  what the node can offer at the chosen concurrency is a self-inflicted outage.
- The existing `sizeLimit`-less emptyDir must be addressed or consciously left
  alone with a reason; declaring a request while the volume stays unbounded is
  half a guarantee.
- No production behaviour change for a deployment that never reaches the cap.
- Cluster-affecting: applied with explicit go-ahead.

## Open questions

1. **What does one build actually use?** This is the whole task and I do not yet
   know. The repo worktree is 287M excluding `.git`, but the Nix closure is the
   variable and Swift's is large. Measuring it means running a build and watching
   the pod's ephemeral usage — which costs an agent run. Whether to spend that, or
   to derive a bound another way, is the first decision.
2. **What number?** Deliberately unanswered until (1). I am not going to defend 10
   again on the same absent evidence.
3. **Does #1608's warm Nix store change the arithmetic?** If gates stop
   re-fetching closures into the container layer, per-build disk drops
   substantially — so these two issues interact, and measuring before #1608 lands
   may measure the wrong thing.
4. **Should the cap be per-tenant rather than global?** `MAX_CONCURRENT_TASKS` is
   global; on a multi-tenant control plane one tenant can consume every slot.
   Out of scope here, but worth recording rather than silently preserving.
