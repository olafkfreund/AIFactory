---
title: Self-hosted runner gVisor setup
sidebar_position: 11
---

# Self-hosted runner gVisor setup

## Why a self-hosted runner?

> **Note (AIFactory#1381):** a self-hosted runner is **no longer required**
> to run the gVisor smoke test. This page previously claimed that Docker
> capability restrictions stopped `runsc` from launching inside Kind node
> containers on GitHub-hosted runners. That diagnosis was wrong — the actual
> fault was that the workflow wrote its containerd config to
> `/etc/containerd/config.d/`, which the Kind node image never imports.
> Writing the stanza directly into `/etc/containerd/config.toml` makes
> gVisor work on stock GitHub-hosted runners, verified end to end against
> `kindest/node:v1.30.0`. `gvisor-smoke.yml` now runs on pull requests,
> a weekly schedule, and manual dispatch.

The setup below is still useful when you want to validate gVisor against a
**production-like host kernel** rather than a Kind node, for example before
enabling `sandbox.gvisor.enabled=true` on a real cluster. Two options:

1. A **self-hosted runner** with gVisor installed on the bare metal / VM host
   — instructions below.
2. A **managed Kubernetes cluster with a gVisor node pool** (GKE Sandbox, EKS
   Bottlerocket with gVisor) — run the test suite directly against that cluster
   without Kind at all.

## Setting up a self-hosted runner with gVisor

### Prerequisites

| Requirement | Notes |
|---|---|
| Ubuntu 22.04 LTS host (bare metal or VM) | Not inside Docker |
| Kernel 5.15+ | Recommended; gVisor works from 4.14 but 5.15+ has better syscall coverage |
| containerd 1.7+ | Not Docker-only — containerd as a standalone daemon |
| GitHub Actions runner binary | v2.300+ recommended |

### Step 1: Install gVisor

```bash
# Add the gVisor apt repository.
curl -fsSL https://gvisor.dev/archive.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) \
  signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] \
  https://storage.googleapis.com/gvisor/releases release main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list

sudo apt-get update && sudo apt-get install -y runsc

# Verify.
runsc --version
which containerd-shim-runsc-v1
```

### Step 2: Configure containerd

```bash
# Add the gVisor runtime handler to containerd's config.
sudo mkdir -p /etc/containerd/config.d

sudo tee /etc/containerd/config.d/gvisor.toml <<'EOF'
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc.options]
  TypeUrl = "io.containerd.runsc.v1.options"
EOF

sudo systemctl restart containerd
sudo systemctl is-active containerd
```

### Step 3: Install and register the GitHub Actions runner

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner

# Download the runner binary (check for latest version at
# https://github.com/actions/runner/releases).
curl -o actions-runner-linux-x64-2.320.0.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.320.0/actions-runner-linux-x64-2.320.0.tar.gz
tar xzf ./actions-runner-linux-x64-2.320.0.tar.gz

# Configure the runner. Get the token from:
# Settings > Actions > Runners > New self-hosted runner > Linux.
./config.sh \
  --url https://github.com/olafkfreund/AIFactory \
  --token <YOUR_RUNNER_REGISTRATION_TOKEN> \
  --labels gvisor,linux,x64 \
  --name aifactory-gvisor-runner-01

# Install as a systemd service so it restarts automatically.
sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status
```

### Step 4: Update the workflow to target the runner

Modify `.github/workflows/gvisor-smoke.yml` to use the self-hosted runner:

```yaml
jobs:
  gvisor-smoke:
    name: gVisor live-cluster smoke (Kind + runsc)
    runs-on: [self-hosted, gvisor, linux, x64]  # was: ubuntu-22.04
```

Also add the `push` trigger back:

```yaml
on:
  push:
    branches: [dev, main]
  pull_request:
    branches: [dev]
  workflow_dispatch:
```

### Step 5: Verify the setup

After the runner is registered, trigger the workflow manually:

```bash
gh workflow run gvisor-smoke.yml
```

A successful run proves gVisor works end-to-end:
- RuntimeClass wiring (chart-level)
- Live pod scheduling under gVisor
- git clone, curl HTTPS, coreutils all working under runsc

## Alternative: run against a managed cluster

If you have a cluster with a gVisor node pool (e.g. GKE Sandbox, EKS with
Bottlerocket + gVisor), you can run the test suite directly without Kind:

```bash
# Point at the real cluster.
export KUBECONFIG=~/.kube/your-cluster.yaml
export GVISOR_NAMESPACE=aifactory
export GVISOR_COMPAT_POD=gvisor-compat-tester

# Deploy AIFactory with gVisor enabled.
helm install aifactory charts/aifactory/ \
  --namespace aifactory --create-namespace \
  --set sandbox.gvisor.enabled=true \
  ...

# Launch a test pod with runtimeClassName=gvisor.
kubectl run gvisor-compat-tester \
  --image=alpine:3.19 \
  --namespace=aifactory \
  --restart=Never \
  --overrides='{"spec":{"runtimeClassName":"gvisor"}}' \
  -- sleep 3600

# Wait for it to reach Running.
kubectl wait pod/gvisor-compat-tester \
  -n aifactory --for=condition=Ready --timeout=120s

# Install tools.
kubectl exec -n aifactory gvisor-compat-tester -- \
  apk add --quiet git curl

# Run the smoke tests.
pytest tests/helm/test_live_gvisor.py -m gvisor_live -v
```

## Related

- CI workflow: `.github/workflows/gvisor-smoke.yml`
- Test suite: `tests/helm/test_live_gvisor.py`
- Operator local guide: [Running the gVisor smoke test locally](./gvisor-smoke-test-local.md)
- Concept doc: [gVisor sandboxing](./gvisor-sandbox.md)
