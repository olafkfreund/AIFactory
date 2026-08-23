---
title: gVisor sandboxing
sidebar_position: 9
---

# gVisor sandboxing

> *Stronger pod isolation for agent workloads — opt-in. Stacks on top of the existing non-root + dropped-caps + read-only-root-fs hardening.*

AIFactory's agent pods already run under multiple layers of defence: non-root user (`65532`), dropped Linux capabilities, read-only root filesystem, `RuntimeDefault` seccomp profile. That's strong against a misbehaving agent process but doesn't help against a kernel-exploit chain — agents share the host kernel with every other workload on the node.

**gVisor closes that gap.** It runs a user-space kernel (`runsc`) between the workload and the host kernel, filtering and emulating syscalls so a kernel exploit in the workload doesn't reach the host. Useful in deployments where you can't fully trust the agent (multi-tenant SaaS, third-party plugins, anything where a compromised agent would let an attacker reach other tenants' data).

Disabled by default. Enable with one `values.yaml` line.

## When to enable

You want gVisor if **any** of these apply:

- You run AIFactory in a **multi-tenant cluster** where other tenants share the node.
- Your **threat model includes a compromised agent escaping to the host** (e.g. an issue body that primes the agent into running attacker-controlled code).
- You're on a regulated cluster where security audit requires **defence-in-depth beyond seccomp**.

You **don't** need it if:

- You're on a single-tenant cluster you fully control.
- The 5-15% CPU overhead is more painful than the threat-model gain.
- Your bash allowlist already restricts the agent to a tightly-scoped set of commands.

## Prerequisites

gVisor is **not** something the chart can install — it's a node-level component. Before you enable the values toggle:

1. **Install `runsc`** on every node that runs AIFactory pods. [gVisor's install docs](https://gvisor.dev/docs/user_guide/install/) cover the package + containerd config. Most managed K8s offerings (GKE, EKS with Karpenter+Bottlerocket) support gVisor either natively or via a node-pool flag.
2. **Create a `RuntimeClass`** in the cluster:

   ```yaml
   apiVersion: node.k8s.io/v1
   kind: RuntimeClass
   metadata:
     name: gvisor
   handler: runsc
   ```

3. (Optional) Confirm `runsc` works by deploying a test pod with `runtimeClassName: gvisor` and checking `dmesg` shows the runsc events.

## Enable in AIFactory

```yaml
# values.yaml
sandbox:
  gvisor:
    enabled: true
    # Override only if your cluster's RuntimeClass is named differently.
    runtimeClassName: gvisor
```

`helm upgrade` and the next pod rollout will land with `runtimeClassName: gvisor` on the pod spec. The existing securityContext (non-root, dropped caps, read-only-root-fs, RuntimeDefault seccomp) stays in place — gVisor stacks on top.

## Compatibility with AIFactory's workloads

The agent's bash allowlist exercises a relatively narrow syscall surface. Real-world workloads tested under gVisor:

The "works" rows in the table below are validated by the live-cluster smoke test (`.github/workflows/gvisor-smoke.yml`). Each row maps to a test in `tests/helm/test_live_gvisor.py`. The blocked rows are by design — gVisor intentionally rejects these syscall patterns.

| Workload | Status | Validated by |
|---|---|---|
| `git clone`, `git pull`, `git push` | works | `test_git_clone_works_under_gvisor` |
| `curl`, `wget` HTTPS calls | works | `test_curl_https_works_under_gvisor` |
| `npm install`, `pnpm install` | works | `test_bash_allowlist_compatibility_matrix` |
| `python -m pip install` | works | `test_bash_allowlist_compatibility_matrix` |
| `apt-get install` inside a Dockerfile build | works (overlayfs translation handled by runsc) | manual |
| `pytest`, `jest`, `go test` | works | `test_bash_allowlist_compatibility_matrix` |
| Workspace PVC read/write | works | `test_workspace_pvc_mount_works_under_gvisor` |
| Outbound HTTPS egress | works | `test_outbound_https_works_under_gvisor` |
| `docker build` (DinD) inside the agent | **not supported** — gVisor blocks nested-container syscalls. Use buildah or kaniko instead. | not tested (expected failure) |
| `tcpdump`, `wireshark` | blocked — by design, BPF programs cross the sandbox boundary. | not tested (expected failure) |
| Direct `/dev/kvm` access | blocked — VM-in-pod workloads are out of scope for AIFactory anyway. | not tested (expected failure) |
| Profiler tools using `perf_event_open` | partial — most counters work; PMU access is filtered. | not tested |

### Compatibility validation

The smoke test (`.github/workflows/gvisor-smoke.yml`) runs on pull requests that touch the chart, the live-gVisor tests, or this page, plus a weekly schedule and manual dispatch.

This page previously stated that gVisor could not run on GitHub-hosted runners, because Docker capability restrictions stopped `runsc` from launching inside Kind node containers. **That was incorrect** (AIFactory#1381). The real cause of the failures was a configuration bug: the workflow wrote the runsc runtime stanza to `/etc/containerd/config.d/gvisor.toml`, but the Kind node image's `/etc/containerd/config.toml` has no `imports` directive and no `config.d` directory, so containerd never read it and reported `no runtime for "runsc" is configured`.

Writing the stanza directly into `config.toml` fixes it. Verified against `kindest/node:v1.30.0`: containerd then registers the `runsc` handler, a pod with `runtimeClassName: gvisor` reaches `Running`, and its `dmesg` shows the `Starting gVisor...` banner — so the workload genuinely executes under the gVisor kernel. `kubectl exec`, DNS and outbound TLS all work inside that sandbox.

Validation paths on GitHub-hosted runners:
- **Exec-based compatibility tests** run against a `gvisor-compat-tester` pod that the workflow launches under `runtimeClassName: gvisor`. The workflow fails if that pod does not become Ready, or if its `dmesg` lacks the gVisor banner, so a silent fallback to `runc` cannot read as a pass.
- **Kubernetes-level wiring tests** (`test_runtime_class_exists_in_cluster`, `test_pods_have_gvisor_runtimeclass`): these inspect API object specs.
- **Template-rendering tests** in `tests/helm/test_gvisor_runtime_class.py` run in the existing `helm-acceptance` CI job on every push.

A self-hosted runner or a managed gVisor node pool remains an option for validating against a production-like kernel, but is no longer required to exercise the compatibility table. See:
- [Running the smoke test locally with Kind](./gvisor-smoke-test-local.md) — for operators with full-kernel access
- [Self-hosted runner gVisor setup](./self-hosted-runner-gvisor-setup.md) — to add a self-hosted runner to the CI pipeline

The most common pain point is **container-build-inside-the-agent** workflows. If your project's CI runs `docker build` from inside a task, you'll see syscall failures under gVisor. The two clean options are:

1. Use **kaniko** (no privileged operations) or **buildah --storage-driver=vfs** for the build step.
2. Disable gVisor on the namespace where AIFactory runs (acceptable if that namespace is single-tenant within the wider cluster).

## Performance cost

gVisor's syscall interposition is not free. Typical overhead, measured on the AIFactory CI workload:

| Metric | Baseline (RuntimeDefault) | gVisor (`runsc`) | Delta |
|---|---|---|---|
| Spec → planner phase wall time | 18s | 19.5s | +8% |
| Coder phase wall time | 4m12s | 4m38s | +10% |
| Pod startup time | 2.1s | 5.8s | +175% |

Pod startup dominates the overhead percentage but the absolute number is still small — single-digit seconds. The per-task wall-time impact (~10%) is the number to budget around.

## What gVisor does NOT replace

- **The bash allowlist.** gVisor doesn't know "this command is in your allowlist". It only filters syscalls. The allowlist is still the first defence.
- **NetworkPolicies.** gVisor isolates the kernel surface, not the network. If the agent shouldn't be able to call your internal DB, you still want a NetworkPolicy.
- **Audit logging.** All MCP write actions still go to the `AuditLog` table; gVisor only affects what the workload can do, not what AIFactory records about it.

## Related

- Epic [#35](https://github.com/olafkfreund/AIFactory/issues/35) — Enterprise v1.1 hardening
- Issue [#37](https://github.com/olafkfreund/AIFactory/issues/37) — chart wiring (closed)
- Issue [#170](https://github.com/olafkfreund/AIFactory/issues/170) — live-cluster CI smoke test (this doc)
- Threat model doc pending [#160](https://github.com/olafkfreund/AIFactory/issues/160) backfill

## References

- [gVisor official docs](https://gvisor.dev/docs/)
- [Kubernetes RuntimeClass spec](https://kubernetes.io/docs/concepts/containers/runtime-class/)
- [GKE Sandbox setup](https://cloud.google.com/kubernetes-engine/docs/concepts/sandbox-pods)
