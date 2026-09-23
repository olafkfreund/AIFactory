---
status: approved
issue: 1465
intent: intent/2026-09-23-1465-one-deploy-one-rollout.md
---

# Spec: One deploy writes one gitops commit

## Design

**Join the two pins in CI**, so a deploy produces exactly one gitops commit and therefore
one ArgoCD sync and one rollout. The cluster side is left alone.

`deploy.yml` becomes three jobs:

1. **`changes`** — decides whether the `-nix` build image needs rebuilding, using plain git
   (`git diff --name-only ${{ github.event.before }} ${{ github.sha }}`) against the same
   paths `build-nix.yml` filters on today (`Dockerfile`, `apps/backend/**`). The repo has no
   path-filter action and does not need one. On `workflow_dispatch` (no `before`), it says yes.
2. **`build-push`** — today's `build-push-bump` **minus its gitops step**: build, push, sign
   the app image, and expose the tag as an output.
3. **`build-nix`** — today's `build-nix.yml` job body, `if: needs.changes.outputs.nix == 'true'`,
   exposing its `-nix` tag as an output.
4. **`pin`** — `needs: [build-push, build-nix]`, writes **one** commit to factory-gitops
   setting the app image and, when `build-nix` ran, `AIFACTORY_BUILD_IMAGE`. It keeps the
   existing verify-after-push check (`deploy.yml:196`) for every value it wrote.

`seam-check` moves to `needs: pin`. `build-nix.yml` is reduced to `workflow_dispatch` only, so
a `-nix` image can still be rebuilt and pinned by hand; its `push` trigger and its own pin step
go away, because that is the second writer.

**Failure semantics** (the intent's constraint that one image's failure must not ship the
other's pin): `pin` runs only if both upstream jobs succeeded or were *skipped*. A **failed**
`build-nix` fails the deploy and writes nothing; a **skipped** one is normal (the paths did
not change) and the commit carries the app pin alone, exactly as today.

**Cost:** a deploy that rebuilds `-nix` now takes as long as the slower build (9-11 min
measured) instead of 4-5. That is not new latency in practice — today the system is not
settled until the second rollout lands 4-6 minutes later — and #1559 makes a deploy a
deliberate act, where minutes matter less than a restart landing on a live build.

## Alternatives rejected

- **Debounce or a sync window in ArgoCD.** Keeps CI untouched, but ArgoCD's sync windows are
  time-of-day rules, not a debounce; it would delay every sync including urgent ones, and it
  hides the double write rather than removing it.
- **Let `build-nix.yml` write both pins** (it finishes last). It does not run on every push, so
  a non-backend deploy would never pin the app image.
- **Keep two commits, one sync.** Nothing available makes ArgoCD coalesce two commits reliably;
  this is the same as the debounce above with more moving parts.
- **Do nothing until #1559 lands.** #1559's own spec calls this out: with deploys gated on a
  version bump, `build-nix.yml`'s pin would still fire on backend pushes and restart pods. The
  restart moves rather than disappears.

## Risks

- **A flaky `-nix` build now blocks the app deploy.** That is the intended trade (never ship
  half a pin), but it couples two builds that were independent. Mitigation: the `-nix` job keeps
  its own generous timeout, and `workflow_dispatch` can ship the app alone in an emergency.
- **The `-nix` pin can go stale if the path filter is wrong.** That drift is exactly what #856
  automated away. The `changes` job must use the same path list as the workflow it replaces,
  and the plan verifies both a touching and a non-touching push.
- **`github.event.before` is unreliable** for force-pushes and first pushes; the job falls back
  to "rebuild" when it cannot compute a diff (fail safe, not fail cheap).
- Moving the gitops step into its own job changes which job holds the `contents: write` token
  for factory-gitops; the permissions must move with it.

## Verification

- A backend push to `main`: **one** gitops commit, one ArgoCD sync, the AIFactory ReplicaSet
  generation advances **once**. (Today: two commits, two syncs — factory-gitops `4d46ea1` then
  `3b58108`, ArgoCD history 17:01:51 then 17:06:59.)
- A non-backend push: `build-nix` is skipped, one commit carrying the app pin only,
  `AIFACTORY_BUILD_IMAGE` unchanged.
- A deliberately failed `-nix` build: no gitops commit at all, and the deploy is red.
- `workflow_dispatch` on `build-nix.yml` still rebuilds and pins the `-nix` image alone.
- After the live deploy, a build running across it survives (the failure #1465 reports).
