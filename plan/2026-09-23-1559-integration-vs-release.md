---
status: draft
issue: 1559
spec: spec/2026-09-23-1559-integration-vs-release.md
---

# Plan: Deploy on a version bump, plus a manual escape hatch

## Decisions (carried from the approved intent and spec)

- `deploy.yml` keeps its `push: branches: [main]` trigger but **exits early when
  `package.json`'s version is unchanged** against the parent commit — the same test
  `release.yml` already applies (`release.yml:42-63`).
- `workflow_dispatch` gains an optional `ref`, so any commit can be shipped deliberately
  without a version bump.
- Nothing about *what* is deployed changes: same images, tags, signatures, pin format.
- Measured context: 28 commits reached `main` in 14 days, every one deploying and restarting
  pods, while only 4 cut a version release.

## Dependencies and order

1. **#1465 (PR #1587) merges first.** Without it `build-nix.yml` still pins on every backend
   push, so the restart this removes would simply move. #1587 also restructures the same file,
   so this plan is written against its four-job shape (`changes` → `build-push`/`build-nix` →
   `pin` → `seam-check`) and must be rebased onto it.
2. **The drift watchdog must change in the Factory hub BEFORE this ships** (step 1 below), or
   it alarms permanently — see the risk in the spec, now confirmed in code.

## Steps

1. **Factory hub** (`olafkfreund/Factory`, `.github/workflows/deploy-drift.yml`): today the
   comparator resolves "the newest commit that should have deployed" as `main`'s newest
   non-docs first-parent commit (`:106`) and compares it with the pinned tag. Under this
   change a commit deliberately does not deploy unless it bumps the version, so add an input
   `deploys_on: push|version-bump` (default `push`, so the other three services are
   unaffected). With `version-bump`, resolve instead the newest first-parent commit on `main`
   whose `package.json` version differs from its parent's. Everything else — grace period,
   the blind-watchdog failure on a missing PAT — is untouched.
   → verify: hub CI green; the new branch of the resolver unit-checked against a repo where
   the tip did not bump (it must resolve the older, released commit, not the tip).
2. **AIFactory** `.github/workflows/deploy-drift.yml`: pass `deploys_on: version-bump` to the
   hub workflow.
   → verify: one scheduled run after step 3 lands is green while `main`'s tip is undeployed.
3. `.github/workflows/deploy.yml`: in the `changes` job (from #1587), add a second output
   `deploy: true|false`:
   - `workflow_dispatch` → always `true`;
   - otherwise compare `jq -r .version package.json` at `${{ github.sha }}` against the parent
     commit, exactly as `release.yml:42-63` does; unchanged → `false`.
   Gate `build-push`, `build-nix` and `pin` on `needs.changes.outputs.deploy == 'true'`, and
   make the job's summary say plainly why it skipped ("version unchanged (3.6.83) — not
   deploying; bump the version or run the workflow manually").
   → verify by step 6.
4. Same file: add the `workflow_dispatch` input `ref` (string, optional, default empty). When
   set, the checkout steps use it and `changes` reports `deploy=true`. Keep the existing
   `main`-only gate on `pin` from #1587, so a dispatch on a branch still cannot pin.
   → verify: `actionlint`; step 6's dispatch.
5. `RELEASE.md`: document both paths — a release deploys (bump the version, as today, §"Creating
   a Release"), and a hotfix without a bump ships via `gh workflow run deploy.yml -f ref=<sha>`.
   Say plainly that landing on `main` no longer deploys, because that is the change people will
   trip over.
   → verify: the file describes exactly what the workflows do.
6. **Dry run before merge**, as with #1587:
   - `actionlint` on both AIFactory workflows;
   - `gh workflow run deploy.yml --ref <branch>` → the jobs run and `pin` skips (branch gate),
     proving the dispatch path still builds;
   - a commit on the branch that does **not** touch `package.json`, pushed to the branch, must
     resolve `deploy=false` in the `changes` job's log (the branch has no `push` trigger, so
     read the logic's decision from a dispatch with a crafted `before`, or assert it locally
     with the same `jq`/`git show` commands the step runs).
   → verify: the recorded outputs match the table in "Tests" below.

## Tests

No unit tests exist for workflows; the evidence is `actionlint`, the dry run, and then the
first pushes after merge:

| push to `main` | expected |
|---|---|
| version unchanged | `changes` reports `deploy=false`; no build, no image, no gitops commit, no rollout |
| version bumped | builds, one gitops commit (#1465), one ArgoCD sync, one rollout; `release.yml` cuts its release as today |
| `workflow_dispatch` (no ref) | deploys `main`'s tip regardless of the version |
| `workflow_dispatch` with `ref` | deploys that commit |

```bash
# after an undeployed commit lands on main:
gh run list --workflow=deploy.yml --branch main --limit 1     # conclusion: success, jobs skipped
git -C factory-gitops log --oneline -1                        # unchanged
kubectl -n factory get rs --sort-by=.metadata.creationTimestamp | tail -2   # no new ReplicaSet
gh run list --workflow=deploy-drift.yml --limit 1             # green, not drifting
```

## Rollback

Revert the AIFactory commit: `deploy.yml` deploys every push again, and the watchdog input
returns to its default. The hub input is additive and defaults to `push`, so it can stay.
Nothing in gitops or the cluster needs undoing.
