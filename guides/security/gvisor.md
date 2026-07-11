# gVisor compatibility note

`values.yaml` ships `sandbox.gvisor.*` (epic #35 child #37): when enabled, the
**control-plane Deployment** pods run under the named `RuntimeClass` (default
`gvisor`). This note records what does and does not work under gVisor,
validated in the Factory sandbox runtime-class evaluation (Factory#274,
`docs/security/sandbox-runtime-class.md` in the Factory repo).

## What works

- The agent's bash-allowlist workload (git, curl, npm install, apt-get).
- The bubblewrap OS sandbox (#363) in its default `fs` mode and opt-in `pid`
  mode — gVisor supports the nested user namespaces bwrap needs, as long as
  the pod stays non-root.
- Network egress, flake evaluation, and binary-cache substitution.
- Overhead is negligible: roughly +1s per pod start.

## What does NOT work

- `sandbox.mode=strict` (bwrap `--unshare-net`): gVisor's netstack rejects the
  netlink call bwrap uses to bring up loopback
  (`bwrap: loopback: Failed RTM_NEWADDR: No child process`). Use `fs`/`pid`
  mode plus a NetworkPolicy for egress control instead.
- **Nix local builds**: the builder child fails to initialize
  (`error: reading a line: Input/output error` while realizing any
  derivation, including the `nix-shell-env` that every `nix develop` needs).
  This is why the per-task build/verify Jobs — whose payload always runs via
  `nix develop` on the nix-runner image — must NOT be scheduled under gVisor.
  The `sandbox.gvisor` toggle therefore applies only to the control-plane
  Deployment, never to the dispatched Jobs.
- tcpdump, BPF programs, nested containers, anything that mmaps `/dev/kvm`.

## Per-task Job pods instead get compensating controls (#812)

Pinned `securityContext` (runAsNonRoot, seccompProfile RuntimeDefault,
allowPrivilegeEscalation false, capabilities drop ALL),
`automountServiceAccountToken: false`, resource/time bounds, and the
`networkPolicy.tasks` policy (default-deny ingress; egress limited to DNS,
public 443, and the service API). Re-evaluate a runtime class for Jobs when
gVisor fixes the Nix builder incompatibility or the cluster substrate can run
Kata (see the Factory decision doc, section 4).
