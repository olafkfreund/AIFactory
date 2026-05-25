# Mirroring the AIFactory image to a private registry

> Audience: platform / SRE teams running AIFactory in an environment that
> cannot pull directly from `ghcr.io` — air-gapped clusters, regulated
> environments with egress allow-lists, or organisations enforcing internal
> registry hygiene.
>
> Goal: copy a released, cosign-signed AIFactory image into your private
> registry **without breaking the signature** — so your Helm chart can keep
> verifying the image came from this project's release pipeline.

## Why this guide exists

AIFactory's release pipeline signs every image with `cosign` keyless via
GitHub OIDC (see `.github/workflows/release.yml` — P0.10 of Epic #26).
The signature lives on the image manifest by reference to the image's
content digest (`sha256:...`).

A naive `docker pull` + `docker push` rewrites the image to a new path
but **drops the cosign signature artifacts**. `cosign verify` against the
mirrored image then fails — which means your `Helm chart`'s pre-deploy
verification breaks, and your audit trail loses the chain back to the
upstream release.

`cosign copy` solves this by copying the image **plus its associated
signature, attestation, and SBOM artifacts** in one transaction. The
content digest is preserved end-to-end, so verification works against
the mirrored URL using the same OIDC identity.

## Prerequisites

- `cosign` CLI v2.2+ installed on the workstation running the mirror
  - `brew install cosign` (macOS) or [install from release](https://github.com/sigstore/cosign/releases)
- Push credentials for the destination registry — `docker login` first
- Read access to `ghcr.io/<owner>/aifactory` (public; no auth needed for
  unauthenticated reads, but rate limits are tighter without)

## Mirror procedure

### 1. Identify the source image by digest

Mirroring **by digest** is mandatory — tags can drift, digests are
immutable. Pull the immutable reference for the version you want:

```sh
SRC=ghcr.io/olafkfreund/aifactory:v1.0.0
DIGEST=$(cosign triangulate --type digest "$SRC")
echo "$DIGEST"
# -> ghcr.io/olafkfreund/aifactory@sha256:abcd1234...
```

Record this `sha256:...` digest in your internal change-management ticket
— it's what your auditors will trace back to the upstream release.

### 2. Copy the image + signature + attestations

```sh
DST=registry.internal.example.com/aifactory:v1.0.0

cosign copy "$DIGEST" "$DST"
```

This single command copies:

- The OCI image index (manifest list — both linux/amd64 and linux/arm64
  manifests are mirrored)
- All layer blobs
- The cosign signature object (`sha256-<digest>.sig` tag)
- The SBOM attestations (SPDX + CycloneDX) attached via `cosign attest`
  in the release pipeline

The destination registry must support the OCI 1.1 referrers API or the
fallback tag scheme — most modern registries do (Harbor 2.5+, ECR,
GHCR, GAR, Quay). Older Distribution-style registries may need a
referrers fallback (see "Common pitfalls" below).

### 3. Verify the mirror preserved the signature

```sh
cosign verify \
  --certificate-identity-regexp "^https://github\.com/olafkfreund/AIFactory/" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  "$DST"
```

This MUST succeed against the mirrored image. The certificate identity
and OIDC issuer are unchanged because cosign keyless signatures bind to
the original signer's identity — not the registry path. If verification
fails, the mirror is broken; investigate before trusting it.

Common verification commands for the SBOM attestation:

```sh
# Download the SBOM that was attested at release time
cosign download attestation "$DST" \
  --predicate-type https://spdx.dev/Document \
  | jq -r '.payload' | base64 -d | jq '.predicate' > sbom.spdx.json
```

### 4. Point the Helm chart at the mirrored image

`values.yaml` (your override file):

```yaml
image:
  repository: registry.internal.example.com/aifactory
  tag: v1.0.0
  # Pin by digest for defence-in-depth; the chart accepts either:
  digest: sha256:abcd1234...
```

The chart's `image.pullPolicy` should be left at `IfNotPresent` for
mirrored images — the registry is your supply-chain perimeter, not the
upstream.

## Common pitfalls

| Pitfall | Fix |
|---|---|
| `cosign copy` succeeds but `cosign verify` against the mirror returns `no matching signatures` | Destination registry doesn't expose referrer artifacts. Use `cosign copy --force` to fall back to the tag-based signature naming convention (`sha256-<digest>.sig`). Verify your registry's referrer support before mirroring. |
| Helm pull fails with `manifest unknown` for one architecture | Source was an OCI image index but destination registry doesn't support multi-arch manifests. Switch to `cosign copy --only=linux/amd64` if you only need one arch, or upgrade the registry. |
| `cosign verify` works locally but fails in cluster | Cluster's egress is blocked from reaching `rekor.sigstore.dev` for transparency log lookups. Either allow-list `rekor.sigstore.dev:443` from cluster nodes, or run a private Rekor witness (advanced). |
| Mirroring an unsigned `:latest` instead of a tagged release | `:latest` is a moving target — don't mirror it. Always mirror by digest from a tagged release (`v1.0.0`). |
| Mirror gets out of date silently | Add a scheduled job that runs `cosign copy` against each new tagged release (the release.yml writes the digest to the release notes; subscribe to release events via GitHub webhook). |

## Auditor cheat-sheet

For a SOC2 / ISO 27001 third-party-component evidence packet, the
mirroring procedure produces:

- **Source digest** (from step 1) — proves provenance back to the
  upstream release in this repo
- **`cosign verify` output** (from step 3) — proves the OIDC identity
  chain is intact; signature wasn't stripped or substituted
- **SBOM JSON** (from step 3 verification command) — components catalogue
  for vulnerability scanning against your CMDB
- **Destination digest** — what's actually deployed; matches source
  digest byte-for-byte

Record all four in the change ticket. Cross-reference with Epic #26
issue #34 (SOC2 evidence pack) when the AIFactory image enters your
production CMDB.

## Disaster recovery

If your private registry loses an image, you can re-mirror at any time
using the same `cosign copy` command — the source upstream image is
immutable by digest, so the result is bit-identical to the original
mirror. Document the source digest in your DR runbook so re-mirroring
is a single command, not an archaeology project.

## See also

- [`Dockerfile`](../../Dockerfile) — the image being mirrored; base
  layers pinned by digest in P0.7
- [`.github/workflows/release.yml`](../../.github/workflows/release.yml)
  — the release pipeline that produces the signed, attested image
- [Sigstore cosign docs](https://docs.sigstore.dev/cosign/) — upstream
  reference for cosign-specific behaviour
