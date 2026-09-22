---
status: draft
issue: 1559
author: Olaf Krasicki-Freund
---

# Intent: Landing a change on main deploys it, so integration costs a deployment

## Problem

Every push to `main` runs `deploy.yml`: build both images, push, cosign-sign, then commit a
new image pin into `factory-gitops`, which ArgoCD syncs and which **restarts the AIFactory
pods**. The only exclusions are `docs/**` and root-level markdown. So landing a one-line fix
costs a full build-and-deploy cycle, and #1559 reports that this slowed the Factory#2586 work
directly. Once the merger opens PRs routinely, merge frequency rises and the cost compounds.

**What the triage corrected.** The issue says a merge forces "a full image build + gitops
pin" *and* a release. The release half is already conditional: `release.yml`'s
`Detect version bump` step compares `package.json` against the parent commit and skips
everything when the version is unchanged. Evidence: 13+ promotions to `main` between 09-08
and 09-11 produced only four version tags. So the cost per merge is the **deploy**
(~4 minutes, plus a gitops commit, an ArgoCD sync and a pod restart), not the 33-minute
release.

That restart is not free either: #1465 records a build being destroyed by a deploy-time
rollout, and #1425 is being held back for a quiet window for the same reason. Coupling every
merge to a deployment is what makes "a quiet window" a scarce resource.

## Proposed outcome

Landing a change on the integration line does not, by itself, deploy it. Deployment becomes
a deliberate act with a moment someone (or something) chooses, so a one-line fix can land
without rebuilding, re-pinning and restarting production, and without waiting for a quiet
window. The always-green guarantee of the integration line is unchanged, and shipping stays
as easy as it is today.

## Affected users and systems

- `.github/workflows/deploy.yml` (build + push + sign + gitops pin) and `release.yml`
  (tag/SBOM on a version bump)
- `factory-gitops` (`apps/aifactory/manifests`) and ArgoCD's sync of it
- The branch model in `CLAUDE.md`: `dev` is the working line, `main` is the release line
- Anyone waiting on a quiet window: #1465, #1425
- The merger's PR flow (Factory#2586), which raises merge frequency

## Constraints

- **No change to what is deployed, only to when.** The same images, signatures and pin format.
- Shipping must not become manual toil: whatever replaces "merge = deploy" needs a one-step
  way to deploy, or an automatic trigger with a different cadence.
- The signature and provenance chain stays intact (Kyverno admits only signed images; the
  digest pin from #1541 depends on it).
- No change to `dev`'s always-green rules, and no change to the CHANGELOG/version gates.
- Rollback must stay as simple as re-pinning a previous digest.

## Open questions

1. **What triggers a deploy instead?** Options: a version bump (deploy follows the release
   that already gates on it); a tag; a manual `workflow_dispatch`; or a schedule that batches
   whatever has landed. Each trades latency against control.
2. **Where does integration live?** `dev` is already the working line, so is this really
   "stop deploying every `main` push", or "promote to `main` less often"? The second needs no
   CI change at all, only a habit.
3. **Does anything today depend on `main` being continuously deployed?** The `sha-<short>`
   tags deploy.yml ships are referenced by the gitops pin; a different cadence changes what
   "latest deployed" means for the drift watchdog and for #1583's release evidence.
