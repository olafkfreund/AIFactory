---
status: approved
issue: 1736
author: olafkfreund
---

# Intent: forward both nix-store vars into the build Job, drop its unseeded store mount

Not fixed on dev. Checked at `e87de74b`. Follow-up (a) from #1620
(`spec/2026-10-10-1620-gate-nix-store-guards.md`, "Follow-ups"). The
blockers it waited on (#1673, #1674, #1677, #1692) have merged; #1692 as
`38ea8a79`.

## Problem

1. **The gate's store guard is inert inside the build Job.** Gates run in
   the build Job and read `AIFACTORY_PACKED_NIX_IN_IMAGE` (`nix_in_image()`,
   `apps/backend/core/nix_env.py:29`) and `AIFACTORY_NIX_STORE_PVC`
   (`apps/backend/agents/gate_runner.py:496-500`). Neither is in
   `_PASSTHROUGH_BUILD_ENV`
   (`apps/web-server/server/services/build_backend.py:259-327`), so inside
   the Job the flag is always off and no store PVC is ever set. A gitops flip
   reaches the build path but not the gate path, which is the drift
   `nix_env.py:44-46` promises cannot happen.
2. **The build Job mounts an unseeded store.** `build_run_py_job_manifest`
   passes `AIFACTORY_NIX_STORE_PVC` (`build_backend.py:659-665`, `:756`) to
   `core/job_dispatch.py:554-562`, which mounts only `/nix/store`, with no
   seed and no `/nix/var/nix/db`. The "warm cache" stays cold, and the
   RWO PVC still pins the Job to one node (#253). The gate path mounts the
   whole `/nix` and seeds it (`apps/backend/core/kube_sandbox.py:202`).

## Proposed outcome

- Inside the build Job, the gate sees the same two values the control plane
  has, so one gitops flip applies to both paths.
- The build Job never mounts the nix-store PVC.

## Affected users and systems

- `kubejob` build backend deployments that set `AIFACTORY_NIX_STORE_PVC`
  and/or `AIFACTORY_PACKED_NIX_IN_IMAGE`.
- Gate Jobs dispatched from inside a build Job.

## Constraints

- Forward both variables together. Forwarding only the PVC brings back the
  #253 node pin.
- `apps/backend/core/job_dispatch.py` is a byte-exact vendored copy of the
  Factory hub canonical (header `:10-21`, `.github/workflows/verification-core-drift.yml`).
  It cannot be edited here without a hub change landing first.
- No cluster changes.

## Open questions

- The issue says to remove the mount in `job_dispatch.py:554-562` and the
  self-check at `:912`. That is a hub change. Proposed: stop the build path
  from passing `nix_store_pvc` instead, which removes the mount from every
  AIFactory build Job, and leave the shared builder's optional mount (also
  vendored by TFactory) to a hub follow-up if wanted.
