---
status: approved
issue: 1583
author: Olaf Krasicki-Freund
---

# Intent: One bad response from a public log fails the whole release

## Problem

The last two `Release` runs failed (2026-09-18, `v3.6.82` and `v3.6.83`), both in
`Tag + GitHub release` → "Generate SBOMs + attest both images with cosign":

```
Error: signing ghcr.io/olafkfreund/aifactory@sha256:8edfb0d8…:
  Post "https://rekor.sigstore.dev/api/v1/log/entries" …
```

That step makes **six** `cosign attest` calls in a row (spdx + cyclonedx × app, rmux, nix)
with **no retry anywhere**. `rekor.sigstore.dev` is a public service outside our control, and
a single bad response fails the run. Every earlier run succeeded, and rekor answers healthy
now (HTTP 200 in 0.2 s), so this was a transient outage, which is exactly the failure mode a
retry exists for.

**What it cost, and what it did not.** Tags `v3.6.82`/`v3.6.83`, both GitHub releases and the
images all exist, and the cluster runs the expected image — delivery was not blocked, and
admission is unaffected because the Kyverno policy verifies image **signatures**, not SBOM
attestations. What is missing is the **SBOM attestation for those two releases**: supply-chain
evidence the pipeline otherwise produces every time. And a workflow that is red for an
unrelated reason hides the next real failure, at ~33 minutes per run to find out.

## Proposed outcome

A transient failure of a public transparency log does not fail a release. The evidence is
either produced or its absence is visible as itself, rather than as a red release whose tag,
images and GitHub release all succeeded. The two releases already missing their attestations
get them.

## Affected users and systems

- `.github/workflows/release.yml`, the `Generate SBOMs + attest both images with cosign` step
- Whoever reads supply-chain evidence for a release (the attestations are the artefact)
- Anyone reading a red `Release` run and having to work out which part actually failed
- `v3.6.82` and `v3.6.83`, whose images are still in GHCR and can still be attested

## Constraints

- **No weakening of what is signed.** Image signatures stay exactly as they are; this is only
  about the SBOM attestations and how their failure is handled.
- A retry must not mask a **persistent** problem: after N attempts it still has to fail, and
  say what it was trying to do.
- The step already takes ~16 minutes (most of it `syft` scanning the 2.25 GB `-nix` image), so
  a retry policy must not multiply that without bound.
- Whatever changes, a release must never report success while its attestations are absent
  **and** unrecorded.

## Open questions

1. **Should a transparency-log outage fail the release at all?** Two defensible shapes:
   - **retry, then fail** — attestation stays a release gate; a long outage blocks releasing;
   - **retry, then continue with a loud, recorded gap** — tag and release proceed, the missing
     attestation is reported (job summary, and the run still visibly not-green), and a re-run
     can fill it in. This needs somewhere the gap is recorded, or it becomes the silent
     absence the intent is against.
2. **Backfill `v3.6.82`/`v3.6.83` now?** The images are still in GHCR, so `cosign attest` can
   be run against them after the fact. Worth doing once as part of this, or left as a separate
   chore?
3. Is the ~16-minute SBOM step worth splitting into its own job (re-runnable alone, parallel
   with the rest) while we are here, or is that scope creep?
