---
status: approved
issue: 2100
author: olafkfreund
---

# Intent: gVisor smoke test must not fail on an apt index hiccup

## Problem

The non-required workflow `gVisor live-cluster smoke` (`.github/workflows/gvisor-smoke.yml`)
failed on release/3.13.0 run 44067273459 at step "Install gVisor runsc + shim on
host". The log shows:
`E: Failed to fetch https://storage.googleapis.com/gvisor/releases/dists/release/main/binary-amd64/Packages.xz File has unexpected size (512 != 516). Mirror sync in progress?`
The gVisor apt repo's index was inconsistent while it was being updated. Run
44065044980 (24 minutes earlier) passed (runsc release-20261109.0). The step also
fetches `gvisor.dev/archive.key` unauthenticated-by-pin, and installs whatever
version the repo serves that day, so the smoke test is not reproducible.

## Proposed outcome

The install step does not depend on the apt index. It downloads a pinned gVisor
release tarball over HTTPS, verifies its sha512, and installs `runsc`,
`containerd-shim-runsc-v1` and the `gvisor-bin/` helpers. The version is pinned
in one place with bump instructions. The workflow stays non-required.

## Affected users and systems

CI only: `gvisor-smoke.yml`. Docs that mention the apt install
(`docs/docs/concepts/gvisor-smoke-test-local.md`, `self-hosted-runner-gvisor-setup.md`)
are for local/self-hosted use and are not changed.

## Constraints

Must keep the downstream step working: it copies `$(which runsc)`, the
`gvisor-bin/` dir next to it, and the shim into the Kind node. Must stay
non-required. No secrets involved.

## Open questions

None. Decision (owner delegation 2026-11-17): pin release binaries with sha512
instead of retry-around-apt, because the failure is a bad index, which a retry
only sometimes outlasts, and pinning also makes the test reproducible.
