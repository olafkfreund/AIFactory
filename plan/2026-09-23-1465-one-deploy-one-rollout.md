---
status: approved
issue: 1465
spec: spec/2026-09-23-1465-one-deploy-one-rollout.md
---

# Plan: One deploy writes one gitops commit

## Decisions (carried from the approved intent and spec)

- Join the two pins **in CI**. One gitops commit per deploy → one ArgoCD sync → one rollout.
- `deploy.yml` becomes `changes` → (`build-push`, `build-nix`) → `pin` → `seam-check`.
- `build-nix.yml` keeps its job and its own pin, but loses its **`push` trigger**; it stays
  usable via `workflow_dispatch` for a manual rebuild.
- A **failed** `-nix` build writes no pin at all and fails the deploy. A **skipped** one is
  normal and the commit carries the app pin alone.
- Nothing about what is deployed changes: same images, tags, signatures, pin format.

## Steps

1. `.github/workflows/deploy.yml`: add a `changes` job (ubuntu, ~2 min) that outputs
   `nix: true|false`. It diffs `${{ github.event.before }}..${{ github.sha }}` for
   `Dockerfile` and `apps/backend/**` — the same paths `build-nix.yml` filters on
   (`build-nix.yml:29-34`). `workflow_dispatch`, an unset/zero `before`, or any git failure →
   `true` (fail safe: rebuild rather than risk a stale pin, the #856 drift).
   → verify: `actionlint .github/workflows/deploy.yml` clean; step 8's dry run prints the
   expected value for a backend and a non-backend commit.
2. Same file: rename `build-push-bump` → `build-push`, and **remove its gitops steps**
   (`Checkout factory-gitops`, `Install kustomize`, `Bump image tag in gitops`,
   `Commit + push the bump`, `Summary` — `:128-221`). Add
   `outputs: {sha: ${{ steps.tags.outputs.sha }}}`. Everything up to and including
   "Assert the signature is where Kyverno looks" stays as-is.
   → verify: actionlint; the job's remaining steps are byte-identical to today's.
3. Same file: add a `build-nix` job — the body of `build-nix.yml`'s job (`:50-119`, checkout →
   assert signature), `needs: changes`, `if: needs.changes.outputs.nix == 'true'`,
   `timeout-minutes: 120`, `outputs: {tag: <sha>-nix}`. Its pin/commit steps are **not** copied.
   → verify: actionlint; the step list matches `build-nix.yml`'s minus the pin steps.
4. Same file: add the `pin` job — `needs: [build-push, build-nix]`,
   `if: always() && needs.build-push.result == 'success' && needs.build-nix.result != 'failure' && needs.build-nix.result != 'cancelled'`.
   It checks out factory-gitops (`GITOPS_PAT`), then:
   - `kustomize edit set image` for the app image (from `deploy.yml:138-140`);
   - when `needs.build-nix.result == 'success'`, the `AIFACTORY_BUILD_IMAGE` sed
     (from `build-nix.yml:127-137`);
   - **one** commit `cd(aifactory): <sha> [skip ci]` listing both values, pushed with the
     existing rebase-and-retry loop (another repo's CD still writes this file — factory-runners
     pins the gate image there);
   - the existing verify-after-push check (`deploy.yml:188-200`) for **each** value written.
   → verify: actionlint; step 8.
5. Same file: `seam-check` → `needs: pin`. Move the `Summary` step into `pin`.
   → verify: actionlint.
6. `.github/workflows/build-nix.yml`: delete the `push:` trigger (`:28-34`), keep
   `workflow_dispatch`. Update the header comment: the automatic pin now lives in `deploy.yml`;
   this workflow is the manual path. Its own pin steps stay for that path.
   → verify: actionlint; `grep -A3 '^on:'` shows only `workflow_dispatch`.
7. Update `deploy.yml`'s header comment to describe the four jobs and why (#1465: two writers,
   two syncs, and the second restart killed a live build).
8. **Dry run before merge**, because this cannot be tested locally end to end:
   - `actionlint` over both workflows;
   - `gh workflow run deploy.yml --ref <this branch>` (dispatch → `changes` says `true`),
     confirming: both builds run, exactly **one** gitops commit appears, and it contains both
     values. If dispatch-on-a-branch would pin gitops from a feature branch, gate the `pin`
     job with `if: github.ref == 'refs/heads/main' || inputs.pin == true` and default that
     input false — decide this while implementing and record it here.
   → verify: the factory-gitops history shows one new commit for the run, not two.

## Tests

There are no unit tests for workflows. The evidence is:

```bash
actionlint .github/workflows/deploy.yml .github/workflows/build-nix.yml
```

and, after merge, on the first backend deploy to `main`:

```bash
git -C factory-gitops log --oneline -3          # ONE cd(aifactory) commit for that sha
kubectl -n argocd get application aifactory -o jsonpath='{range .status.history[-2:]}{.id} {.deployedAt}{"\n"}{end}'
kubectl -n factory get rs --sort-by=.metadata.creationTimestamp | tail -3
```
Expected: one commit, one new ArgoCD history entry, one new ReplicaSet. Today the same push
produces two of each (`4d46ea1`/`3b58108`; history 17:01:51/17:06:59).

## Rollback

Revert the commit: `build-nix.yml` regains its `push` trigger and `deploy.yml` its inline pin,
which is exactly today's behaviour. Nothing in gitops or the cluster needs undoing — the pins
written by either shape are identical in content.
