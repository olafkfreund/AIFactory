---
status: approved
issue: 2100
intent: intent/2026-11-17-2100-gvisor-pinned-runsc.md
---

# Spec: pinned gVisor release install

## Design

Replace the apt key + apt source + `apt-get install runsc` in step "Install
gVisor runsc + shim on host" with:

- env `GVISOR_VERSION: "20261109.0"` and `GVISOR_SHA512` (the version the last
  passing run used), declared on the step with a comment: to bump, pick a
  directory under `gs://gvisor/releases/release/`, fetch
  `<ver>/x86_64/gvisor.tar.bz2.sha512`, update both values.
- `curl -fsSL --retry 5 --retry-all-errors --retry-delay 5` the
  `https://storage.googleapis.com/gvisor/releases/release/$GVISOR_VERSION/x86_64/gvisor.tar.bz2`
  into a temp dir, `sha512sum -c` against the pinned hash (fail on mismatch),
  `sudo tar -xjf ... -C /usr/local/bin` (contains `runsc`,
  `containerd-shim-runsc-v1`, `gvisor-bin/`).
- Keep the existing `which runsc` / `--version` / shim echo lines (drop the
  `dpkg -L` line, which no longer applies). The Kind-node step keeps working
  because `gvisor-bin/` sits next to `runsc` in /usr/local/bin.

Pinned hash for 20261109.0 (verified locally against the downloaded file):
1a6b8f74c37b4931ef473f67ec1f601690855bedc52ac709f2fac965c69677aa0426ef44598d8eee1aed72cfdb9df87a2a5f352cb3348880fc61a7732bc10f2a

## Alternatives rejected

- Retry with backoff around `apt-get update`: the index was wrong, not slow; a
  retry may or may not outlast the repo sync, and the version still floats.
- Individual `runsc`/shim objects: not published for current releases, only
  `gvisor.tar.bz2`/`.zstd`.

## Risks

Pinning means the weekly schedule no longer auto-detects gVisor release drift (only Kind node drift); the schedule comment is reworded and the pin is bumped by hand.
A pinned version ages; the bump instructions live in the step comment. The tarball
is ~85 MB (same size as the apt package). GCS outage still fails the job, as it
did before; the job is non-required.

## Verification

Download and sha512 check done locally; tarball lists `runsc`,
`containerd-shim-runsc-v1`, `gvisor-bin/`. actionlint/yaml parse locally. The
PR's own gVisor smoke run (it triggers on workflow changes if paths include the
file; otherwise via workflow_dispatch) is the end-to-end check.
