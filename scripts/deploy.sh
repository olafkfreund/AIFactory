#!/usr/bin/env bash
# One-command local release for aifactory:
#   build → push (immutable sha tag) → bump factory-gitops → ArgoCD redeploys.
#
# Usage:  scripts/deploy.sh            # build from current checkout
#         TAG=v3.5.0 scripts/deploy.sh # build + also push an explicit tag
#
# Prereqs: docker logged in to ghcr.io (with push), git access to
# olafkfreund/factory-gitops, and (optional) ssh to p510 for an instant sync.
set -euo pipefail

IMAGE="ghcr.io/olafkfreund/aifactory"
GITOPS="git@github.com:olafkfreund/factory-gitops.git"
GITOPS_PATH="apps/aifactory/manifests"
ROOT="$(git rev-parse --show-toplevel)"
SHA="sha-$(git -C "$ROOT" rev-parse --short HEAD)"
EXTRA_TAG="${TAG:-}"

echo "==> Building $IMAGE:$SHA"
docker build -t "$IMAGE:$SHA" -t "$IMAGE:latest" ${EXTRA_TAG:+-t "$IMAGE:$EXTRA_TAG"} -f "$ROOT/Dockerfile" "$ROOT"

echo "==> Pushing"
docker push "$IMAGE:$SHA"
docker push "$IMAGE:latest"
[ -n "$EXTRA_TAG" ] && docker push "$IMAGE:$EXTRA_TAG"

echo "==> Bumping factory-gitops ($GITOPS_PATH → $SHA)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
git clone -q --depth 1 "$GITOPS" "$WORK"
( cd "$WORK/$GITOPS_PATH"
  if command -v kustomize >/dev/null 2>&1; then
    kustomize edit set image "$IMAGE=$IMAGE:$SHA"
  else
    sed -i -E "s#(newTag:).*#\1 $SHA#" kustomization.yaml
  fi )
( cd "$WORK"
  git add "$GITOPS_PATH/kustomization.yaml"
  if git diff --cached --quiet; then echo "   (no change)"; else
    git commit -q -m "cd(aifactory): $SHA [skip ci]"
    git push -q
    echo "   pushed bump to factory-gitops main"
  fi )

echo "==> Triggering ArgoCD sync (best-effort)"
ssh -o BatchMode=yes -o ConnectTimeout=5 p510 \
  'KUBECONFIG=/etc/k3d/kubeconfig kubectl -n argocd patch application aifactory --type merge -p "{\"operation\":{\"sync\":{}}}"' \
  2>/dev/null && echo "   synced" || echo "   (skipped — ArgoCD will auto-sync within ~3m)"

echo "==> Done. aifactory → $IMAGE:$SHA"
