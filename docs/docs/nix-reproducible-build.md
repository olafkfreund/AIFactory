---
title: Reproducible builds in a per-task Nix env
sidebar_position: 12
---

# Building in a per-task Nix flake (RFC-0005 Tier A)

This page explains, from AIFactory's side, how the coder builds and runs verification
gates inside a reproducible per-task Nix environment, and why that matters: AIFactory
builds in the SAME flake TFactory later verifies in, so the build environment and the
verify environment cannot drift.

For the end-to-end fleet picture (PFactory declares the toolchain, TFactory verifies
and captures screenshots) see the hub guide "Reproducible test environments". For how
the plan itself is shown to be buildable and testable, see PFactory's "Planning and
Trust" guide.

## Why a Job, not the coder pod

The coder is a long-lived agent loop (the Claude Agent SDK) running in a single
deployment pod. That pod has no container runtime — it cannot `docker run`. So the
execution of build and verification gates is pushed into an ephemeral Kubernetes Job,
created via the in-cluster API. The agent loop stays in the coder pod; only the
toolchain-bearing work runs in the Job.

The Job backend is `core/kube_sandbox.py` (proven in-cluster). Which backend a gate
uses is selected in `agents/gate_runner.py` by the `AIFACTORY_SANDBOX_BACKEND`
environment variable.

## How the coder creates a build pod and how flake.nix sandboxes it

```mermaid
flowchart LR
    subgraph Pod["Coder pod (Claude Agent SDK) - no container runtime"]
        Agent[Coder loop] --> MAT[core/nix_env.materialize_flake_into:<br/>write flake.nix from contract.environment]
        MAT --> GR[agents/gate_runner: nixjob backend]
        GR --> KS[core/kube_sandbox.KubeJobSandbox]
        KS -- create Job via SA RBAC --> API[(k8s API)]
    end

    API --> Job

    subgraph Job["Ephemeral Job pod (backoffLimit 0, ttlSecondsAfterFinished)"]
        Img[tfactory-runner-nix image<br/>nix + flakes only] --> ND[nix develop path:/work#default -c gate]
        PVC[(worktree co-mounted<br/>subPath -> /work)] --> ND
        ND --> Store[/nix/store: toolchain from<br/>the per-task flake, via cache.nixos.org/]
        Store --> Run[Run the gate hermetically<br/>e.g. pytest, go test, build]
        Run --> Res[exit code + log]
    end

    Res --> Pod
    Job -. auto-GC .-> X[deleted]
```

What flake.nix does as the sandbox boundary:

- `core/nix_env.materialize_flake_into()` writes a `flake.nix` into the task worktree
  from the contract `environment` manifest (the same manifest PFactory emitted and
  TFactory consumes). It respects a repo-owned `flake.nix` unless the manifest marks the
  flake as `generated`.
- `nix develop` realises exactly the pinned dependency closure into `/nix/store` and puts
  only those tools on `PATH`. The image carries only Nix itself — every toolchain comes
  from the flake, so there is no per-language image to maintain.

## The build-gate decision flow

```mermaid
flowchart TD
    A[Trailing gates for a built task] --> B{AIFACTORY_SANDBOX_BACKEND == nixjob?}
    B -- no --> H[Existing behavior unchanged:<br/>host runner / docker / kubejob]
    B -- yes --> M[Materialize flake.nix from<br/>implementation_plan.json environment]
    M --> D[detect gates: lint, test, build, ...]
    D --> W[_nix_wrap each gate:<br/>nix develop path:/work#default -c bash -c gate]
    W --> J[Dispatch via kube_sandbox Job<br/>image = tfactory-runner-nix]
    J --> R[Collect pass/fail per gate]
```

### The `path:/work` detail (do not drop it)

`_nix_wrap` builds `nix develop path:/work#default -c bash -c "<gate>"`. The `path:`
reference is required for a co-mounted git worktree. A bare `/work` reference makes Nix
use its git fetcher, which (1) rejects the repo on a uid mismatch (the Job runs as root,
the worktree files are owned by the non-root service uid) and (2) ignores the untracked,
freshly generated `flake.nix`. `path:` copies the directory directly, sidestepping both.
This was proven the hard way in TFactory's live runs.

## Adoption — what teams and operators do

The Nix build path is opt-in and default-off. With it off, gate behavior is exactly as
before (host runner), so turning it on is a deliberate operational choice.

To build in the per-task Nix env, set on the AIFactory deployment:

```
AIFACTORY_SANDBOX_GATES=1
AIFACTORY_SANDBOX_BACKEND=nixjob
AIFACTORY_SANDBOX_IMAGE=ghcr.io/olafkfreund/tfactory-runner-nix:latest
```

Nothing else is required: `flake.nix` is generated from the contract `environment`, so
teams do not hand-write flakes or maintain language images.

Honest status: the AIFactory-side Nix gate path is implemented and unit-tested (the gate
runner suite is green) and uses the same provisioner and `nix develop path:/work` recipe
that TFactory has already validated live end-to-end (real toolchains and browser
screenshots from a cluster Job). The AIFactory-side live validation is pending the
environment flip above; until an operator enables it, the host runner remains in effect.

## Where it lives

| Concern | Module |
| --- | --- |
| Job backend (create / watch / logs / delete) | `core/kube_sandbox.py` |
| Backend selection + Nix gate wrapper | `agents/gate_runner.py` (`nixjob`, `_nix_wrap`, `_nix_kube_runner`) |
| Materialize flake from the contract | `core/nix_env.py` (`materialize_flake_into`) |
| Flake generator (vendored from the hub) | `core/nix_provisioner.py` |
| Coder hook (materialize before gates) | `agents/coder.py` |
