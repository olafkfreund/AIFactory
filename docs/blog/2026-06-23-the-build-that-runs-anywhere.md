---
slug: the-build-that-runs-anywhere
title: "The build that runs anywhere: cutting the last node-pin"
authors: [olaf]
tags: [ai-coding, devops, kubernetes, nix]
date: 2026-06-23
description: A packed AIFactory build still nailed itself to one node through the Nix store. Baking the store into a -nix build image, behind a dispatcher gate, removed the last node affinity. Here is the exact mechanism, and an honest line on what is proven versus what waits on a second node.
---

When a build can only run on one node, you do not have a cluster — you have one
machine with extra steps. This is the story of removing the last thing that pinned
an AIFactory build to a single node, why it took three small changes instead of
one, and exactly where the honest line sits between "shipped" and "proven."

{/* truncate */}

## Two Jobs, one store

When `AIFACTORY_BUILD_BACKEND=kubejob`, the coder loop (`run.py`) runs as its own
Kubernetes Job instead of an in-pod subprocess. That is the control/execution split
RFC-0016 and RFC-0017 have been building toward: the web-server pod stays small and
long-lived, and the heavy per-task work goes into a short-lived Job the scheduler
can place wherever there is room.

"Wherever there is room" is the whole point — and it is exactly what a mounted
volume takes away. A Kubernetes pod that mounts an RWO `local-path` PersistentVolumeClaim
inherits that volume's node affinity, because a `local-path` PV is physically a
directory on one node. Mount one, and the scheduler has no decision left to make.

A build Job touched two such volumes:

- **`/work`** — the task's git checkout, which the coder writes to.
- **`/nix`** — the toolchain cache. Even though `run.py` itself is not wrapped in
  `nix develop`, the coder runs its inline verification gates through it, against a
  per-task `flake.nix` generated from the contract's environment manifest. So the
  build Job needs a populated `/nix/store` just as much as a gate Job does.

[Workspace pack/unpack](/concepts/workspace-storage) already solved the first:
the producer packs the populated `/work` to object storage and the Job unpacks it
into a writable `emptyDir`, so there is no workspace PVC to pin to. But the build
stayed pinned anyway — through `/nix`.

## Why the store was a volume in the first place

The fast way to give a build a warm `/nix/store` is to share one: an RWX-ish warm
cache PVC so repeat builds do not re-realise the same toolchain closure from
`cache.nixos.org` every time. Warm, fast — and node-pinned, because that cache was an
RWO `local-path` PVC living on the control-plane node. Every packed build Job that
co-mounted it got dragged straight back to that node, no matter what the scheduler
would have preferred.

That was the last wire.

## Make the store a layer, not a mount

A `/nix/store` does not have to arrive as a volume. It can arrive as an image layer.
The change landed as three small, independently reversible pieces:

1. **A dispatcher gate.** `AIFACTORY_PACKED_NIX_IN_IMAGE`, on the packed path only,
   drops the Nix-store PVC from the build Job manifest. Off: nothing changes. On: the
   Job carries no Nix-store volume.

2. **A `-nix` build image.** The ordinary AIFactory runtime image plus a baked
   `/nix/store` (the Dockerfile's `build-runtime` stage). It is published on demand by
   its own workflow — deliberately *not* part of the per-commit deploy, because baking a
   multi-gigabyte store onto every push would tax CD for an image most commits do not
   change.

3. **The image override.** `AIFACTORY_BUILD_IMAGE` points the build Job — and only the
   build Job — at that `-nix` tag. The resolution order is
   `AIFACTORY_BUILD_IMAGE` > `AIFACTORY_IMAGE` > built-in default, so the control plane
   and the gate Jobs keep running their normal images; the override is surgical.

With `AIFACTORY_PACK_WORKSPACE=true`, `AIFACTORY_PACKED_NIX_IN_IMAGE=true`, and
`AIFACTORY_BUILD_IMAGE` on a `-nix` tag, a packed build Job mounts no workspace PVC and
no Nix-store PVC. It carries no node affinity. The scheduler is free to place it
anywhere.

## A note on the default

It is worth being exact, because it is the kind of thing that is easy to overstate.
The shipped *code* default for `AIFACTORY_BUILD_BACKEND` is still `subprocess` — the
in-pod path — kept deliberately as the safe fallback. Job-native execution is the
*deployment* default: gitops sets `kubejob` on the reference cluster. A fresh install
with no flags runs in-pod until an operator opts in. "Live" here means the reference
deployment, not a default baked into the binary.

## The honest line

Shipped and wired into the reference deployment: the gate, the `-nix` image, the
override. The chain is verified at every layer that does not need new hardware — the
image is published and resolvable, the manifest the dispatcher emits for a packed build
has no Nix-store volume, and the toolchain resolves from the baked store. The build Job
is node-agnostic by construction.

Not yet proven: an actual landing on a *different* node. The live cluster is single-node
today, so there is no second node for the scheduler to place a build on. "No node
affinity in the manifest" is a strong structural guarantee, but it is not the same as
watching a packed build run on node B while node A is cordoned. That demonstration waits
on one thing — a second node — and on no more code. When the node exists, the proof is a
five-minute exercise.

We would rather ship the mechanism, mark the line precisely, and show the landing when
the hardware is real. The wire is cut.
