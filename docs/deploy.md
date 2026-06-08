# Deploying AIFactory to the k3d cluster (CD → ArgoCD)

AIFactory runs on the p510 k3d cluster, managed by ArgoCD from
[`olafkfreund/factory-gitops`](https://github.com/olafkfreund/factory-gitops). The deploy loop is
**build → push immutable image → bump the gitops tag → ArgoCD redeploys**.

## The automated loop (push to main)

Every push to `main` runs [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. builds `ghcr.io/olafkfreund/aifactory:sha-<short>` (+ `:latest`), amd64, and pushes to GHCR;
2. checks out `factory-gitops` and runs `kustomize edit set image` on
   `apps/aifactory/manifests/kustomization.yaml` to point at the new `sha-<short>` tag;
3. commits + pushes that bump;
4. ArgoCD (auto-sync + selfHeal) reconciles within ~3 min and rolls the new pod.

Immutable `sha-<short>` tags mean every deploy is uniquely identifiable and reproducible — no
`:latest` ambiguity in the cluster.

### One-time setup

Add a repo secret so CI can write to the gitops repo:

```bash
# fine-grained PAT: repo olafkfreund/factory-gitops, Contents: read+write
gh secret set GITOPS_PAT --repo olafkfreund/AIFactory
```

## Manual one-command release

```bash
scripts/deploy.sh            # build current checkout → push → bump → sync
TAG=v3.5.0 scripts/deploy.sh # also push an explicit semver tag
```

Needs: `docker login ghcr.io` (push), git access to `factory-gitops`, and optionally ssh to `p510`
for an instant `argocd app sync` (otherwise auto-sync picks it up).

## Verify

```bash
ssh p510 'KUBECONFIG=/etc/k3d/kubeconfig kubectl -n factory get pods -l app=aifactory'
ssh p510 'KUBECONFIG=/etc/k3d/kubeconfig kubectl -n argocd get app aifactory'
# rendered tag:
ssh p510 'KUBECONFIG=/etc/k3d/kubeconfig kubectl -n factory get deploy aifactory -o jsonpath="{.spec.template.spec.containers[0].image}"'
```

## Roll back

Revert the bump commit in `factory-gitops` (or `kustomize edit set image …:<older-sha>`), push —
ArgoCD rolls back. Old images remain in GHCR by digest.

## Notes / hardening

- `release.yml` still cuts **semver releases** (tag + SBOM + cosign) on a `package.json` version
  bump; `deploy.yml` is the **continuous** main→cluster path. They coexist.
- Cluster secrets (`factory-secrets`, `ghcr-pull`) are currently seeded out-of-band; they must move
  into the agenix bootstrap so a cluster rebuild doesn't require re-seeding
  (see factory-gitops issue #6).
