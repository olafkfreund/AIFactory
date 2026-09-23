---
status: draft
issue: 1559
intent: intent/2026-09-23-1559-integration-vs-release.md
---

# Spec: Deploying becomes a deliberate act, not a side effect of merging

## What the traffic actually looks like

- `main` took **28 non-merge commits in 14 days**, through ~10 promotion PRs from `dev`
  (all titled `release: …`) plus 2 `release/X.Y.Z` PRs.
- Every one of those pushes ran `deploy.yml`: build, push, sign, pin, **pod restart**.
- Only **4** of them cut a version release (`v3.6.80`-`83`); `release.yml` skipped the rest,
  because it already gates on a `package.json` version change.

So the ceremony is not the problem the issue thinks it is: the 33-minute release is already
conditional. **Every promotion deploying is the problem**, and it is what makes a quiet
window scarce (#1465, #1425).

## Design

**Deploy on a version bump, plus a manual escape hatch.**

1. `deploy.yml` keeps its `push: branches: [main]` trigger but gains the same
   `Detect version bump` step `release.yml` already uses (`release.yml:42-63`: compare
   `package.json` against `HEAD~1`). When the version is unchanged, the job **exits early**
   without building, pinning or restarting anything.
2. `workflow_dispatch` stays, and gains an optional `ref` input, so any commit can be shipped
   deliberately without a version bump — a hotfix, or a re-deploy of the current tip.
3. Nothing else changes: same images, same `sha-<short>` tags, same signatures, the same
   single gitops pin format, the same ArgoCD behaviour. Only the **trigger condition** moves.

Shipping then costs exactly what a release already costs today (`node scripts/bump-version.js`,
a CHANGELOG entry, merge), and landing a change costs nothing. `dev` is untouched: it was
never deployed.

## Alternatives rejected

- **Batch deploys on a schedule.** Predictable, but it makes "what is deployed right now"
  a function of the clock, and a broken deploy is discovered away from the change that caused it.
- **Deploy only from `release/X.Y.Z`.** Same effect as the version gate but adds a branch
  ceremony for a hotfix, and the two recent release branches show the version bump is already
  the real signal.
- **Change nothing in CI; promote to `main` less often.** This is the honest zero-code option
  and it is what the intent's question 2 asks. It is rejected as the *primary* answer because
  it leaves the coupling in place: whoever promotes still pays a restart, and the merger
  (Factory#2586) will promote more often, not less.
- **Deploy on tag.** Equivalent to the version gate, but the tag is created *by* `release.yml`
  after the fact, so the deploy would trail the release by a whole pipeline.

## Risks

- **A change can now sit on `main` undeployed.** That is the point, but "deployed" and "main's
  tip" stop being synonyms. The deploy-drift watchdog (`deploy-drift.yml`, every 30 min) and
  #1583's release evidence both reason about that. The watchdog must compare against the last
  *deployed* version, not `main`'s tip, or it will report permanent drift.
- **A hotfix path must exist and be known**, or someone will bump the version just to ship,
  which is the ceremony this is removing. `workflow_dispatch` covers it; it needs a line in
  the release docs.
- **`build-nix.yml` still pins on every backend push** (#1465). With this change its pin would
  land while the app image is not being redeployed — the pin is inert until the next deploy,
  but it still triggers an ArgoCD sync and a restart. **#1465 must be fixed with or before
  this**, or the restart simply moves rather than disappearing.

## Verification

- A push to `main` with no version change: `deploy.yml` reports the skip, pushes no image,
  writes no gitops commit, and the AIFactory ReplicaSet generation is unchanged.
- A push that bumps the version: deploys exactly as today (image, signature, single pin,
  one rollout), and `release.yml` still cuts its release.
- `workflow_dispatch` on an arbitrary ref deploys that ref.
- The drift watchdog is green after an undeployed commit lands on `main` (its comparison is
  what this change must not break).
