---
status: approved
issue: 1583
intent: intent/2026-09-23-1583-release-sbom-retry.md
---

# Spec: Retry the attestations, and make them re-runnable on their own

## What the structure forces

`release.yml` has **one** job (`release`) that does everything in order: detect the version
bump, validate the CHANGELOG, create the tag, create the GitHub release, build and push three
images, sign them, then generate SBOMs and attest (`:259-281`). The attest step reads digests
from earlier **step** outputs.

That ordering is why the 2026-09-18 failures left the worst possible state: the tag, the
GitHub release and all three images existed, and only the evidence was missing — with the run
red, so nothing distinguished "released, evidence missing" from "release failed".

## Design

Answering the intent's three questions together, because they are one shape:

1. **Retry each attest, then fail (Q1: attestation stays a gate).** A small bounded helper
   wraps each `cosign attest`: 3 attempts, 5s → 20s → 60s. A transient rekor response is
   absorbed; a persistent one still fails, naming the image and predicate it could not attest.
   `syft scan` is local work and is not retried.
2. **Split SBOMs + attestation into its own job (Q3: yes, and it is not scope creep — it is
   what makes recovery cheap).** New job `sbom-attest`, `needs: release`, consuming the three
   digests as **job outputs** from `release` (which today only exist as step outputs). Effects:
   - a failed attestation no longer paints the whole release red — the tag/release/images job
     is green, and the missing evidence is the red thing, which is the truth;
   - it can be re-run alone (`gh run rerun --job`), so recovery costs ~16 minutes, not 33;
   - the ~16 minutes of `syft` scanning stop sitting between the release and its completion.
3. **A dispatch path for backfill (Q2: yes, backfill — and this is how).** `release.yml` gains
   `workflow_dispatch` with optional `app_digest`, `rmux_digest`, `nix_digest`. When given,
   only `sbom-attest` runs, against exactly those digests. This is the only way to attest an
   existing image, because cosign keyless needs the workflow's OIDC identity — it cannot be
   done from a laptop. It doubles as the recovery path for the next outage.
   `v3.6.82` and `v3.6.83` are then backfilled by dispatching it once per release.

Nothing about image **signing** changes. The certificate identity stays
`^https://github\.com/<owner>/AIFactory/`, which the new job satisfies like any other, so
Kyverno admission is unaffected (it verifies signatures, not attestations).

## Alternatives rejected

- **Retry, then continue green.** The release would report success with evidence missing,
  which is the silent absence the intent rules out.
- **Retry only, no split.** Cheapest, but a persistent outage still costs a 33-minute red run
  to rediscover, and there is still no way to backfill an existing release.
- **`continue-on-error` on the attest step.** The file's own history warns against exactly
  this: `continue-on-error` hid a broken OpenAPI generation for 20+ releases (`:271` comment).
- **Drop rekor (`--tlog-upload=false`).** It would end the failure class by removing the
  transparency-log entry, which is most of what the attestation is worth.

## Risks

- **Two jobs mean the images are pushed before the evidence exists**, and now visibly so.
  That is already true today; the split makes the window explicit rather than creating it.
- **The dispatch path attests arbitrary digests.** It must verify each digest exists in this
  repo's package before attesting, or a typo produces an attestation against something
  unintended.
- **Re-attesting an already-attested image** adds a second attestation rather than failing.
  That is cosign's behaviour; the backfill must therefore be run once per release, knowingly.
- A retry lengthens the worst case: 6 attests × 3 attempts × up to 60s ≈ 5 extra minutes at
  the tail. Acceptable against a 33-minute release.

## Verification

- **The failure path:** point `COSIGN_REKOR_URL` (or an equivalent unreachable endpoint) at a
  dead host in a scratch run and confirm the helper retries 3 times, then fails naming the
  image — not a silent pass.
- **The happy path:** the next real release produces attestations for all three images, and
  `cosign verify-attestation --type spdxjson` succeeds against each.
- **The backfill:** dispatch with `v3.6.83`'s three digests, then
  `cosign verify-attestation` against those images succeeds where it currently fails.
- `actionlint` clean; the `release` job's other steps byte-identical.
