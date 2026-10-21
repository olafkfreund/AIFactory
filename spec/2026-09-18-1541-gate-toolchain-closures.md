---
status: approved
issue: 1541
intent: intent/2026-09-18-1541-gate-toolchain-closures.md
---

# Spec: Finish option A (warm gate toolchains), verify it, stop it drifting

## What research found (the intent's premise has moved)

Option A has **already been built and shipped** for two of the three gate languages.
The issue was never closed:

- `olafkfreund/factory-runners` PR #8 (`d6a00af`, merged 2026-09-11, "warm the Kotlin and
  Swift closures into the image (AIFactory#1541)") added `kotlin` and `swift` dev shells to
  `docker/factory-runner-nix/warmup/flake.nix`. The Dockerfile realises each into its own
  profile gcroot, beside the Python one from TFactory#768.
- It is published as `factory-runner-nix:sha-d6a00af` = **`:latest`**
  (`sha256:c0813874…`, 2026-09-11).
- **Gate Jobs run that image.** The gate image is `AIFACTORY_SANDBOX_IMAGE`
  (`agents/gate_runner.py:532`), which `factory-gitops`
  (`apps/aifactory/manifests/manifests.yaml:393`) sets to `factory-runner-nix:latest`, and the
  k3d node caches no copy, so every gate pulls the current `:latest`.
- **nixpkgs rev:** `factory-runners` guards its warm-up rev against the hub's
  `DEFAULT_NIXPKGS` (`assert-warmup-nixpkgs-lockstep.sh`). AIFactory's vendored
  `nix_provisioner.py` is byte-matched to the hub by a required check. Both are
  `567a49d1…`, so there is no rev drift.
- **Measured cost:** the image went from 0.55 GB to **2.25 GB** compressed (73 → 75 layers).

**Two gaps remain.**

1. **Swift is only partly warm.** For `swift`, AIFactory's `generate_flake` emits `swift`,
   `swiftpm`, `swiftPackages.XCTest` **plus `swiftPackages.Dispatch` and
   `swiftPackages.Foundation`**. The warm-up shell has only the first three. Every Swift
   gate still downloads the rest, and Swift overrunning the deadline is the failure #1541
   reports. Kotlin matches exactly (`kotlin gradle jdk21`).
2. **Nothing stops the lists drifting.** `factory-runners/tests/test_nix_warmup.py`
   hand-keeps the per-language package lists (`:28-29`). A generator change in the hub
   silently turns a warm gate cold again. The only symptom would be slower gates, and
   eventually a deadline kill.

## Design

1. **`factory-runners`:** add `swiftPackages.Dispatch` and `swiftPackages.Foundation` to
   the `swift` warm-up shell. Replace the hand-kept lists in `test_nix_warmup.py` with a
   check **derived from the hub's `nix_provisioner.generate_flake`**. For each gated
   language (python/kotlin/swift), the packages the generator emits must be a subset of
   that language's warm-up shell. It fetches the hub canonical the same way the existing
   rev guard does.
2. **`factory-gitops`:** pin `AIFACTORY_SANDBOX_IMAGE` to the new image **by digest**
   instead of `:latest`. A mutable tag means any `factory-runners` push silently changes
   what every gate runs, and the Kyverno image-signature policy already lists this image.
   Future warm-up changes then land as a reviewed digest bump. This needs a quiet window,
   since it restarts pods (#1425/#1465).
3. **AIFactory's own `Dockerfile:484-485` pin stays unchanged, on purpose.** It copies the
   whole `/nix/store` into the AIFactory runtime image. Since #1525 gates run in their own
   Job on the gate image, AIFactory's image doesn't need the language closures, and bumping
   it would add about 1.7 GB for nothing. A comment beside the pin records this, so nobody
   "fixes" the mismatch later.
4. **AIFactory:** no code change beyond that comment.

## Alternatives rejected

- **Bump AIFactory's Dockerfile pin to match.** It costs about 1.7 GB per AIFactory image
  and serves no path that runs gates any more.
- **Keep `:latest` in gitops.** Reproducibility and signature admission both want a digest;
  `:latest` is how an unreviewed image change reaches gates.
- **Trim the generator's Swift packages to match the warm-up.** Dispatch and Foundation are
  there because Swift projects need them. Warming them is the correct direction.
- **B / C from the intent.** Superseded by approval of A, and A is mostly in place.

## Risks

- The Swift warm-up grows the image further; the plan measures the new size. If Foundation
  pulls in a GTK-sized closure, that is where #1541's "Swift drags in GTK" came from, and the
  size is the price of a gate that fits the deadline.
- The digest pin in gitops adds a manual bump step for every warm-up change. That is the
  point, but it has to be documented in `factory-runners`' release notes or README.
- This spans three repos (`factory-runners`, `factory-gitops`, AIFactory). The order matters:
  publish the image, verify it, then pin its digest.

## Verification

- `factory-runners` CI: the new derived-subset test passes for python/kotlin/swift, and it
  fails if a generator package is removed from a warm-up shell (mutation).
- After the image publishes, a live gate run on the cluster for a Kotlin fixture and a Swift
  fixture. Neither pod log fetches a toolchain path from `cache.nixos.org`, and both finish
  inside the default deadline. This is the check that closes #1541.
- The gitops change is applied in a quiet window, and a later gate pod's `imageID` shows the
  pinned digest.
