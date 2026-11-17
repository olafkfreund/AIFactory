---
status: approved
issue: 2100
spec: spec/2026-11-17-2100-gvisor-pinned-runsc.md
coders:
  1: lead
reviewers:
  - codex
---

# Plan: pinned gVisor release install

Decisions carried from the spec: pin gVisor 20261109.0 tarball with
sha512, curl retries, install to /usr/local/bin, remove the apt path, keep the
job non-required, document the bump in the step comment.

## Steps

1. `.github/workflows/gvisor-smoke.yml` step "Install gVisor runsc + shim on host"
   (~lines 99-123): replace the body with: `set -euo pipefail`; step `env:`
   `GVISOR_VERSION: "20261109.0"` and `GVISOR_SHA512: 1a6b8f74c37b4931ef473f67ec1f601690855bedc52ac709f2fac965c69677aa0426ef44598d8eee1aed72cfdb9df87a2a5f352cb3348880fc61a7732bc10f2a`;
   `curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 -o "$RUNNER_TEMP/gvisor.tar.bz2" https://storage.googleapis.com/gvisor/releases/release/${GVISOR_VERSION}/x86_64/gvisor.tar.bz2`;
   `echo "${GVISOR_SHA512}  $RUNNER_TEMP/gvisor.tar.bz2" | sha512sum -c -`;
   `sudo tar -xjf "$RUNNER_TEMP/gvisor.tar.bz2" -C /usr/local/bin`; keep the
   `which runsc` / `runsc --version` / shim echo lines, drop `dpkg -L`. Update the
   section comment (no apt; bump instructions: new dir under
   gs://gvisor/releases/release/, update both env values from `<ver>/x86_64/gvisor.tar.bz2.sha512`).
   Also reword the schedule comment (lines ~48-49): the weekly run now catches
   Kind node image drift; gVisor drift is caught when the pin is bumped.
   Do not touch other steps.
   → verify by `python3 -c 'import yaml;yaml.safe_load(open(".github/workflows/gvisor-smoke.yml"))'`,
   actionlint if present, and a local run of download + sha512sum -c + tar listing.
   Traps: step runs under `bash -e`; keep `set -euo pipefail`; hash must be a
   hard-coded literal, never fetched from the same bucket at run time.

## Tests

Local: sha512sum -c passes; tar lists runsc, shim, gvisor-bin/. CI: PR checks
green; the gVisor smoke workflow is non-required.

## Rollback

Revert the commit (restores the apt install).
