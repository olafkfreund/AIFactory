---
status: draft
issue: 1541
spec: spec/2026-09-18-1541-gate-toolchain-closures.md
---

# Plan: Finish option A (warm gate toolchains), verify it, stop it drifting

## Decisions (carried from the approved intent and spec)

- Option A: toolchain closures are baked into `factory-runner-nix`. Python and Kotlin are
  already warm and match exactly (`factory-runners` #8, image `sha-d6a00af`,
  `sha256:c0813874…`, 2.25 GB compressed).
- **Swift gap:** add `swiftPackages.Dispatch` and `swiftPackages.Foundation` to the warm-up
  `swift` shell.
- **Drift guard:** the warm-up package lists are checked against the hub's
  `nix_provisioner.generate_flake` output. Hand-kept lists are removed.
- **gitops:** `AIFACTORY_SANDBOX_IMAGE` is pinned by digest, not `:latest`. It is applied
  in a quiet window.
- **AIFactory `Dockerfile:484-485` pin stays**, with a comment explaining why (gates don't
  run in the AIFactory image; bumping costs about 1.7 GB).
- The order across repos: publish image → verify live → pin digest.

## Steps

1. **`factory-runners`** (branch `feat/warm-swift-foundation-1541`):
   `docker/factory-runner-nix/warmup/flake.nix`, `swift` shell: add
   `pkgs.swiftPackages.Dispatch` and `pkgs.swiftPackages.Foundation`.
   → verify: `nix flake check path:docker/factory-runner-nix/warmup` evaluates locally.
2. **`factory-runners`:** new `.github/scripts/assert-warmup-covers-generated.py`. It fetches
   the hub `nix_provisioner.py` from `HUB_RAW_PROVISIONER`, the same source as
   `assert-warmup-nixpkgs-lockstep.sh`, calls `generate_flake` for the python, kotlin and
   swift manifests the fleet uses, extracts the `pkgs.<name>` package set of each, and fails
   if any is missing from that language's warm-up shell (python → `default`). It is wired
   into the workflow next to the rev guard. `tests/test_nix_warmup.py` drops its hand-kept
   `kotlin`/`swift` lists (`:28-29`) in favour of this check.
   → verify: it passes after step 1. **Mutation:** removing `swiftPackages.Foundation` from
   the flake makes it fail and name the missing package.
3. **`factory-runners`:** open the PR. CI builds the image, runs its three-command smoke
   (including the warm `nix develop`), and the new guard runs. Record the new compressed
   size in the PR description (it was 2.25 GB).
   → verify: CI green. **Merge needs your approval**; on merge, CI publishes
   `sha-<commit>` and `:latest`.
4. **Live verification on the cluster**, without a paid agent run. Generate AIFactory's actual
   Kotlin and Swift flakes locally (`core.nix_provisioner.generate_flake`, as in the spec's
   probe), ship them as a ConfigMap, and run one throwaway Job per language on the **new
   digest**, with the same securityContext as a gate Job:
   `nix develop --option substitute false path:/w#default --command <swift|kotlin> --version`.
   With substitution off, it succeeds **only** if the whole closure is already in the image.
   → verify: both Jobs succeed well inside the default gate deadline, and the Job logs show no
   `copying path` lines. Delete the Jobs and ConfigMap afterwards. This result is what closes #1541.
5. **`factory-gitops`:** `apps/aifactory/manifests/manifests.yaml:393`: set
   `AIFACTORY_SANDBOX_IMAGE` to `ghcr.io/olafkfreund/factory-runner-nix@sha256:<new digest>`.
   Check the Kyverno signature policy (`apps/kyverno-policies/.../verify-factory-image-signatures.yaml`)
   still admits a digest reference. **Applied in a quiet window, with your go-ahead.** It
   restarts AIFactory pods (#1425/#1465).
   → verify: after sync, the next gate pod's `imageID` equals the pinned digest.
6. **AIFactory** (this branch): add a comment beside `Dockerfile:484-485` saying this
   `/nix/store` copy intentionally lags the gate image: gates run in their own Job on
   `AIFACTORY_SANDBOX_IMAGE`, and this copy only needs the base tooling. Reference #1541.
   Also add a line to `factory-runners`' README: a warm-up change needs a gitops digest bump.
   → verify: the `Dockerfile` builds unchanged (comment only), and CI is green.
7. Close #1541 with the step 4 evidence (Job logs showing success with substitution off, and
   timings).

## Tests

- `factory-runners` CI: the rev guard, the new generated-package guard, `tests/test_nix_warmup.py`,
  and the image smoke.
- The live Jobs from step 4, succeeding with `--option substitute false`.
- AIFactory CI on the comment-only change.

## Rollback

- The image: repoint gitops to the previous digest (`c0813874…`, or `:latest` as before).
  The old image remains in GHCR.
- The guard script is CI-only; revert its PR.
- The AIFactory change is a comment; revert it.
