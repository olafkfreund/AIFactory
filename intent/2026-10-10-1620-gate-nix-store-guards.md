---
status: approved
issue: 1620
author: olafkfreund
---

# Intent: gate Nix store guards and comments do not match reality

## Problem

Checked at `6ae5b646` on dev: #1620 is not fixed. No commit or PR references it.
Nothing is broken at runtime today, but the code describes the system wrongly
and its store guard is dead. That has already sent two investigations
(#1608, #1560) the wrong way.

1. **False comments.** `apps/backend/agents/gate_runner.py:41-45`, `:499-501`
   and `:532-535`, plus the docstring at `tests/test_gate_runner_nix.py:341-344`,
   say the runner image bakes no language closures, so Swift cannot finish within
   the deadline. The issue measured the live image: it holds swift, kotlin, gradle
   and openjdk-21, and `nix develop` on a Swift flake finishes in 4s.
   `apps/backend/core/nix_env.py:38-41` already says the `-nix` images bake the
   store, so the two modules contradict each other.
2. **Gate and build apply the guard to opposite paths.** `gate_runner.py:503`
   drops the store PVC when `nix_in_image()` is true, but only the co-mount call
   (`:561-566`) uses that value; the packed call (`:536-541`) passes the raw
   `configured_store_pvc`. The build path does the reverse: it drops the PVC on
   the packed path only (`build_backend.py:664`). The gate-side bypass is
   deliberate (#1541, comment `:491-497`) and tested
   (`tests/test_gate_runner_nix.py:341`), but its rationale rests on the false
   claim in item 1.
3. **The guard can never fire inside the build Job.** Neither
   `AIFACTORY_PACKED_NIX_IN_IMAGE` nor `AIFACTORY_NIX_STORE_PVC` is in
   `_PASSTHROUGH_BUILD_ENV` (`apps/web-server/server/services/build_backend.py:259-327`).
   The build path reads both on the server, where they are set. The gate runs
   inside the build Job, where `nix_in_image()` is always False and no store PVC
   is ever set. So the promise in `nix_env.py:44-46` ("one gitops flip cannot land
   on one path and silently miss the other") does not hold for the gate.
   It is latent only because both variables are missing; forwarding one without
   the other brings back the #253 node pin.
4. **Gate and build mount the store differently.** The gate mounts `/nix` and
   seeds it from the image (`apps/backend/core/kube_sandbox.py:202-245`). The build
   mounts only `/nix/store` with no seed (`apps/backend/core/job_dispatch.py:554-562`),
   so there is no `/nix/var/nix/db` and the "warm cache" stays cold.
   The self-check at `job_dispatch.py:912` locks this layout in.
5. **Swift gate failures are undiagnosed.** The deadline is 600s, finished gate
   Jobs are TTL-reaped after 120s, and the issue found no surviving logs. #1608
   found the store PVC was never provisioned. The likely cause (SPM fetching over
   git into `.build/`) is a hypothesis.

## Proposed outcome

- Comments and docstrings describe what the image and the Job env actually hold.
- The store guard either works on every path that reads it, or is gone.
  No path silently ignores a flag that another path honours.
- The build path's store mount is either useful (seeded, with a db) or removed.
- The Swift gate failure has a recorded cause, or a tracked task to find one.

## Affected users and systems

- Maintainers and agents debugging gate failures (items 1-3).
- Operators who flip `AIFACTORY_PACKED_NIX_IN_IMAGE` or set
  `AIFACTORY_NIX_STORE_PVC` (items 2-4).
- Build Jobs that mount the store PVC (item 4).
- End users with Swift projects, whose gates fail (item 5).
- Files: `gate_runner.py`, `nix_env.py`, `kube_sandbox.py`, `job_dispatch.py`,
  `build_backend.py`, `tests/test_gate_runner_nix.py`.

## Constraints

- Must not bring back the RWO node pin on packed Jobs (RFC-0017 #190,
  Factory#253). `build-nix.yml:7,32,178` pairs the image rollout with
  `AIFACTORY_PACKED_NIX_IN_IMAGE=true`.
- Items 2 and 3 interact: changing either alone can make the guard live on one
  path and not the other.
- `_PASSTHROUGH_BUILD_ENV` is a security surface under review in #1674.
  Any addition must keep `tests/test_agent_env_scrub.py` passing.
- `job_dispatch.py:912` asserts the build mount set; changing item 4 changes
  that check and its tests.
- New comment text must be checked against the live image, so one false claim
  is not replaced by another.
- Any revived store PVC needs what the issue lists: 20Gi, RWX on NFS, the nightly
  GC CronJob deleted in gitops commit `3047d37` (not in this repo), and store
  flags in `_nix_wrap`.
- Item 5 needs live cluster access (`kubectl get jobs -w`), which agents lack.
- Overlap: #1674 and #1692 edit `_PASSTHROUGH_BUILD_ENV`; #1673 edits
  `build_backend.py`; #1677 touches `tests/test_build_backend_kubejob.py`.

## Open questions

1. Guard (items 2-3): forward `AIFACTORY_PACKED_NIX_IN_IMAGE` (and the store
   PVC) into the build Job and apply it on the packed gate path, or delete the
   gate-side suppression?
2. Build store mount (item 4): drop it, since the image already holds the
   toolchains, or keep it and accept the 20Gi RWX NFS PVC plus GC CronJob?
3. Scope: fix items 1-4 here and split item 5 into a separate ops task, or keep
   #1620 open until Swift is diagnosed?
4. Ordering: wait for #1674 and #1673 to merge before touching
   `build_backend.py`, or rebase onto them?
5. Item 5: who runs the cluster probe, and when?
