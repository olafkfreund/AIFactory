---
status: approved
issue: 1541
author: Olaf Krasicki-Freund
---

# Intent: Every gate cold-downloads its whole toolchain

## Problem

The gate runner image carries **no language closures**. Every gate Job fetches its entire
toolchain from `cache.nixos.org` on every run (#1541, verified in a throwaway pod on
`factory-runner-nix`). Kotlin's closure fits inside the 600 s Job deadline. Swift's does not,
because it pulls in GTK, so `swift-unit` dies mid-download, **even on a Kotlin-only change**.

What exists and why it isn't enough:

- **#1542** made the deadline configurable, and a Job killed by its deadline is no longer
  reported as a failing test. That is honest, but it doesn't make the gate run.
- **#1543** honours `AIFACTORY_NIX_STORE_PVC` on the packed path
  (`services/build_backend.py:150`), but it stays **inert**: the env var is unset and no
  `aifactory-nix-store` PVC exists. `AIFACTORY_PACKED_NIX_IN_IMAGE=true`
  (`core/nix_env.py:25-28`) drops the warm store entirely.
- The warm store was removed for **two** reasons (#1541's comment): an RWO scheduling
  deadlock across nodes (gone now that the cluster is single-node), and **one mount
  serialising concurrent Jobs** (TFactory#623), which capped the fleet at 3–4 concurrent
  tasks on 80 idle cores. Simply recreating the PVC would bring the second one back.

So every gate pays minutes of download per run, and some gates cannot run at all.

## Proposed outcome

A gate for any supported language starts with its toolchain already present, finishes
inside its deadline, and doesn't serialise concurrent gates. A Kotlin-only change never
fails because Swift couldn't download.

## Affected users and systems

- The gate runner image (`factory-runner-nix`) and its build pipeline (image size, build time)
- `core/nix_env.py`, `services/build_backend.py`, `core/job_dispatch.py` (gate Job spec)
- `factory-gitops` (env vars, and possibly a PVC)
- Throughput: the concurrency ceiling that #1425 wants to raise

## Constraints

- Must not reintroduce the RWO single-mount serialisation (TFactory#623) or a cross-node
  scheduling deadlock.
- Gates stay hermetic. A cached closure must be the same derivation a fresh fetch would
  produce, pinned to the flake's nixpkgs rev.
- The image-size/build-time cost must be stated and acceptable before it ships.
- Gitops changes restart pods, so they go in a quiet window (#1425, #1465).

## Open questions

1. **Which option?** The issue's own analysis:
   - **A. Bake the supported languages' closures into `factory-runner-nix`.** Deterministic,
     no shared mount, and matches the documented "both Job paths resolve /nix from the
     image". Costs image size and build time. **Recommended by the issue.**
   - **B. One writer, many readers.** A warmer Job fills an RWX PVC, and gates mount it
     read-only as a substituter with a per-Job `/tmp` store. Avoids both removal reasons, at
     the cost of another moving part.
   - **C. Status quo plus a larger deadline.** Cheapest; keeps paying minutes per gate.
2. For A: which languages go in the image, all supported ones or the ones seen in real
   builds, and what image-size ceiling is acceptable?

## Decisions (at approval, 2026-09-18)

1. **Option A:** bake the supported languages' toolchain closures into `factory-runner-nix`.
2. Which languages, and the image-size ceiling, are for the spec to propose from
   measurement. They are not assumed here.
