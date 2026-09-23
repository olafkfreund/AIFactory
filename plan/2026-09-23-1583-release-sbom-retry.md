---
status: draft
issue: 1583
spec: spec/2026-09-23-1583-release-sbom-retry.md
---

# Plan: Retry the attestations, and make them re-runnable on their own

## Decisions (carried from the approved intent and spec)

- Each `cosign attest` retries 3 times (5s → 20s → 60s) and then **fails**, naming the image
  and predicate. Attestation stays a release gate. `syft` is local work and is not retried.
- SBOMs + attestation move to their own job, `sbom-attest` (`needs: release`), so a failed
  attestation leaves the release job green and the *evidence* red, and can be re-run alone.
- A `workflow_dispatch` path attests an existing release, because cosign keyless needs the
  workflow's OIDC identity. This is both the backfill for `v3.6.82`/`v3.6.83` and the recovery
  path for the next outage.
- No change to image signing, the certificate identity, or Kyverno admission.

**Refinement found while planning (spec said "digest inputs"):** the release images are
**tagged by version** — `v3.6.83`, `v3.6.83-rmux`, `v3.6.83-nix` (the last is
`sha256:8edfb0d8…`, exactly the digest in the failed run's error). So the dispatch takes a
**version** and resolves the three tags itself. That is friendlier than pasting three digests
and it satisfies the spec's "verify the digest exists in this repo's package" for free:
resolution fails loudly if a tag is absent.

## Steps

1. `.github/workflows/release.yml`: add to the `release` job
   `outputs: {app: ${{ steps.build.outputs.digest }}, rmux: ${{ steps.build_rmux.outputs.digest }},
   nix: ${{ steps.build_nix.outputs.digest }}, version: ${{ steps.detect.outputs.new }}}`.
   Step ids already exist (`build`, `build_rmux`, `build_nix`, `detect`).
   → verify: `actionlint`.
2. Same file: **move** the "Install Syft" and "Generate SBOMs + attest…" steps (`:255-281`)
   out of `release` into a new job `sbom-attest`:
   - `needs: release`, `if: always() && needs.release.result == 'success'` for the push path;
   - `permissions: {contents: read, packages: write, id-token: write, attestations: write}`;
   - it installs cosign + syft, then resolves its three image refs from the job outputs (push)
     or from the dispatch input (step 4);
   - `timeout-minutes: 45` (the step measured ~16 min, most of it scanning the 2.25 GB `-nix`).
   → verify: `actionlint`; the moved steps are byte-identical apart from where the refs come from.
3. Same file: add the retry helper inside that job and wrap **each** `cosign attest`:
   ```bash
   attest() {  # $1=type $2=predicate $3=image
     for delay in 5 20 60 stop; do
       cosign attest --yes --type "$1" --predicate "$2" "$3" && return 0
       [ "$delay" = stop ] && break
       echo "::warning::attest $1 for $3 failed; retrying in ${delay}s (rekor is a public service)"
       sleep "$delay"
     done
     echo "::error::could not attest $1 for $3 after 3 attempts"
     return 1
   }
   ```
   with `set -euo pipefail` kept, so an exhausted retry still fails the job.
   → verify: `actionlint`; step 6's failure-path test.
4. Same file: `workflow_dispatch` gains `version` (string, optional, e.g. `3.6.83`). When set,
   `release` is skipped (`if: github.event_name == 'push'`) and `sbom-attest` resolves
   `ghcr.io/<owner>/aifactory:v<version>`, `…:v<version>-rmux`, `…:v<version>-nix` to digests
   with `crane digest` (or `docker buildx imagetools inspect`), failing loudly if a tag is
   missing. It attests **by digest**, never by tag.
   → verify: `actionlint`; step 6's backfill run.
5. Header comment: why the split exists (#1583 — the run went red with the tag, release and
   images already created, so "released, evidence missing" looked like "release failed"), and
   that re-attesting adds a second attestation, so the dispatch is a once-per-release action.
6. **Verification runs** (workflows cannot be tested locally):
   - `actionlint`;
   - **failure path**: a scratch branch run of `sbom-attest` with `COSIGN_REKOR_URL` pointed at
     an unreachable host — expect 3 attempts, the `::error::` line, and a failed job;
   - **backfill**: dispatch with `version: 3.6.83`, then
     `cosign verify-attestation --type spdxjson --certificate-identity-regexp '^https://github\.com/olafkfreund/AIFactory/' --certificate-oidc-issuer https://token.actions.githubusercontent.com ghcr.io/olafkfreund/aifactory@sha256:3ba64aad41fa…`
     succeeds where it fails today. Repeat for `3.6.82`.
   → verify: the recorded outputs, and `gh run view` showing `release` skipped on the dispatch.

## Tests

```bash
actionlint .github/workflows/release.yml
# after the backfill dispatch, for each of the three images of v3.6.83 and v3.6.82:
cosign verify-attestation --type spdxjson \
  --certificate-identity-regexp '^https://github\.com/olafkfreund/AIFactory/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "<image@digest>"
```
Expected: verification succeeds for all six; it fails for all six today.

## Rollback

Revert the commit: the SBOM steps return inside `release` with no retry, which is today's
behaviour. Attestations already written by the backfill stay — they are additive evidence
against a digest and nothing depends on their absence.
