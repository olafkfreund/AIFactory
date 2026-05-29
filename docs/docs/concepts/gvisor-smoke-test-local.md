---
title: Running the gVisor smoke test locally
sidebar_position: 10
---

# Running the gVisor smoke test locally

This page is for operators who want to validate that their cluster's gVisor
setup is correct before deploying AIFactory to production, or for engineers
who want to run the `gvisor_live` test suite locally without waiting for CI.

The same test suite (`tests/helm/test_live_gvisor.py -m gvisor_live`) that
runs in the `gvisor-smoke.yml` CI workflow can be run against any cluster
that has gVisor installed and a `gvisor` RuntimeClass registered.

## Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| `kind` | v0.24.0 | `go install sigs.k8s.io/kind@v0.24.0` or the [kind releases page](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) |
| `kubectl` | v1.30.0 | [kubectl install docs](https://kubernetes.io/docs/tasks/tools/) |
| `helm` | 3.16.0 | `brew install helm` or [helm.sh](https://helm.sh/docs/intro/install/) |
| `runsc` (gVisor) | latest | see step 1 below |
| Python 3.12 + uv | — | `pip install uv` |

## Step 1: Install runsc on the host

```bash
# Add the gVisor apt repository (Ubuntu/Debian).
curl -fsSL https://gvisor.dev/archive.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) \
  signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] \
  https://storage.googleapis.com/gvisor/releases release main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list

sudo apt-get update && sudo apt-get install -y runsc
runsc --version
```

For macOS or other distributions see the
[gVisor install docs](https://gvisor.dev/docs/user_guide/install/).

## Step 2: Create a Kind cluster

```bash
cat <<'EOF' | kind create cluster --name gvisor-local --config=-
apiVersion: kind.x-k8s.io/v1alpha4
kind: Cluster
nodes:
  - role: control-plane
EOF
```

## Step 3: Install the runsc shim inside the Kind node

```bash
NODE="gvisor-local-control-plane"

# Copy the host runsc binary into the Kind node container.
docker cp "$(which runsc)" "${NODE}:/usr/local/bin/runsc"
docker exec "${NODE}" chmod +x /usr/local/bin/runsc

# Register the gVisor runtime handler with containerd.
docker exec "${NODE}" bash -c 'cat >> /etc/containerd/config.toml <<TOML

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
  TypeUrl = "io.containerd.runsc.v1.options"
TOML'

# Restart containerd so the new runtime block is picked up.
docker exec "${NODE}" systemctl restart containerd
sleep 5
docker exec "${NODE}" systemctl is-active containerd
```

## Step 4: Create the gVisor RuntimeClass

```bash
kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
EOF

kubectl get runtimeclass gvisor
```

## Step 5: Deploy AIFactory with gVisor enabled

```bash
# Pull chart dependencies first (LiteLLM sub-chart needs its tarball).
helm dep update charts/aifactory/

helm install aifactory charts/aifactory/ \
  --namespace aifactory \
  --create-namespace \
  --set sandbox.gvisor.enabled=true \
  --set postgres.bundled=true \
  --set postgres.externalSecretName="" \
  --set externalSecrets.enabled=false \
  --set oidc.enabled=false \
  --set workspaces.enabled=true \
  --set image.repository=busybox \
  --set image.tag=latest \
  --set image.pullPolicy=IfNotPresent \
  --timeout=5m \
  --wait=false

# Wait for pod to come up (gVisor startup takes ~5-6 s extra).
kubectl wait deployment/aifactory \
  --namespace aifactory \
  --for=condition=Available \
  --timeout=240s

kubectl get pods -n aifactory -o wide
```

## Step 6: Install test dependencies

```bash
cd apps/backend
uv venv
uv pip install -r ../../tests/requirements-test.txt
uv pip install "kubernetes==30.1.0"
```

## Step 7: Run the smoke tests

```bash
KUBECONFIG=~/.kube/config \
GVISOR_NAMESPACE=aifactory \
  apps/backend/.venv/bin/pytest \
    tests/helm/test_live_gvisor.py \
    -m gvisor_live \
    -v \
    --timeout=120
```

Expected output: all five test classes pass. If any fail, the test
output includes the kubectl exec stderr so you can identify which
syscall gVisor rejected.

## Step 8: Teardown

```bash
kind delete cluster --name gvisor-local
```

## Troubleshooting

**Pod stuck in `RuntimeClass not found` event**

The RuntimeClass was not created before the pod was scheduled. Run:

```bash
kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
EOF
kubectl rollout restart deployment/aifactory -n aifactory
```

**containerd does not recognise runsc after restart**

Check that the `[plugins."io.containerd.grpc.v1.cri"...]` block was
appended correctly:

```bash
docker exec gvisor-local-control-plane \
  cat /etc/containerd/config.toml | grep -A5 runsc
```

If the block is missing, the `docker exec bash -c` here-doc may have
been truncated. Re-run step 3 manually.

**Test `test_workspace_pvc_mount_works_under_gvisor` skipped**

AIFactory was installed without `workspaces.enabled=true`. Re-install
with that flag set, or skip the test manually with
`--deselect tests/helm/test_live_gvisor.py::TestGvisorCompatibilityMatrix::test_workspace_pvc_mount_works_under_gvisor`.

## Running against a real cluster (not Kind)

If you have a cluster that already has gVisor nodes, skip steps 2-3
and point `KUBECONFIG` at your real cluster's kubeconfig. Ensure:

- `runsc` is installed on every node in the node pool that will
  schedule AIFactory pods.
- The `gvisor` RuntimeClass exists in the cluster.
- The namespace and Helm release name match the `GVISOR_NAMESPACE`
  environment variable.

## Related

- CI workflow: `.github/workflows/gvisor-smoke.yml`
- Test suite: `tests/helm/test_live_gvisor.py`
- Concept doc: [gVisor sandboxing](./gvisor-sandbox.md)
