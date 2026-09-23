---
status: approved
issue: 1465
author: Olaf Krasicki-Freund
---

# Intent: One deploy restarts the pods twice, and the second one destroys live work

## Problem

A push to `main` that touches the backend starts **two** workflows that each commit a pin
for `apps/aifactory` in factory-gitops:

| workflow | commit | changes |
|---|---|---|
| `deploy.yml` | `cd(aifactory): <sha>` | the app **image** tag |
| `build-nix.yml` | `cd(aifactory): pin AIFACTORY_BUILD_IMAGE <sha>-nix` | an **env var** (the packed build image) |

On the same commit, `deploy.yml` finishes in 4-5 minutes and `build-nix.yml` in 9-11
(measured over four recent releases), so the two commits land **4-6 minutes apart**. ArgoCD
syncs each, and the Deployment rolls twice.

The second roll is the damaging one: it is an env-var change, so it restarts pods while the
first rollout's build is already running. #1465 records exactly that — a build killed at the
requirements stage, leaving a spec dir with `requirements.json` and no plan or code, which
looks like a build that simply produced nothing. Evidence for the mechanism: factory-gitops
`4d46ea1` then `3b58108` on one `main` commit, and ArgoCD history id 60 at 17:01:51 then id
61 at 17:06:59. It also explains the report's own puzzle that the image was unchanged across
both rolls.

`build-nix.yml` already anticipated the shared repo — "the pin push retries on conflict" —
but a retry fixes the *push*, not the second *sync*.

This is also what makes a quiet window scarce: #1425 is held back waiting for one, and
#1559 is about the same coupling from the other end.

## Proposed outcome

One deploy restarts the pods once. A build that is running when a deploy lands survives it,
or is stopped deliberately rather than by a second rollout nobody asked for. Both pins still
reach gitops on every relevant push, and neither can drift (the drift `build-nix.yml` was
written to fix, #856, stays fixed).

## Affected users and systems

- `.github/workflows/deploy.yml` and `.github/workflows/build-nix.yml`
- `factory-gitops` `apps/aifactory/manifests` and ArgoCD's auto-sync of it
- Any build running during a deploy — the failure this issue reports
- Held work: #1425 (waiting for a quiet window), #1559 (merge implies deploy)

## Constraints

- **Both pins must still land.** `AIFACTORY_BUILD_IMAGE` drifted 18 days before it was
  automated (#856); nothing here may reintroduce that.
- A failure in one image's build must not silently ship the other's pin.
- No change to what is deployed, only to how many rollouts it causes.
- The `-nix` image is multi-GB and deliberately not built on every push, so the fix cannot
  assume both workflows always run.
- Keep the signature/provenance chain and the digest-pin behaviour from #1541 intact.

## Open questions

1. **Where should the two pins be joined?** In CI (one workflow waits for the other and
   writes a single commit; costs deploy latency, roughly the 9-11 minute `-nix` build), or in
   the cluster (an ArgoCD sync window or debounce, so commits within N minutes roll once;
   keeps CI as-is but delays every sync).
2. **When `build-nix.yml` does not run** (a push that touches neither `Dockerfile` nor
   `apps/backend/**`), should the deploy still wait, or ship immediately?
3. **Is a rollout that interrupts a build acceptable at all**, once it happens only once?
   Draining or refusing to roll while a build is in flight is a larger change, and may belong
   with #1559 rather than here.
