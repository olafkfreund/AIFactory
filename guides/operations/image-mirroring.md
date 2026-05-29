# Image mirroring for private + air-gapped deployments

> Audience: Platform operators running AIFactory in environments where pods cannot pull from the public internet (air-gapped clusters, regulated banks, defence networks, on-prem PoVs behind a corporate proxy).
> Companion drill script: `scripts/drills/image-mirroring.sh` (Epic #26 P7.6).
> Status: GA in v1.0; instructions verified against cosign 2.2+ and the GHCR-published v1.0.0 release.

## Why mirroring matters

AIFactory publishes container images to `ghcr.io/olafkfreund/aifactory:<version>` with a cosign signature, an SBOM attestation, and SLSA-3 build provenance attached to the image digest. Three operator scenarios need an alternative source:

1. **Air-gapped clusters.** Nodes have no route to `ghcr.io`. The deployment cluster pulls only from an internal registry (Harbor, JFrog Artifactory, Nexus, AWS ECR, Azure ACR, GCP Artifact Registry).
2. **Supply-chain attestation lockdown.** A regulator requires every running image to come from a registry the organisation controls so the org can prove the image bits never changed between upstream verification and pod start.
3. **Pull-rate / egress cost.** Large fleets pull the same image thousands of times per day; an internal mirror avoids hitting GHCR's anonymous-pull rate cap and removes per-pull egress charges.

In all three cases the **signature, SBOM, and provenance attestation must travel with the image bytes** — otherwise downstream `cosign verify` fails, and you have lost the entire supply-chain story.

`docker pull && docker tag && docker push` does NOT copy the cosign signature. You need `cosign copy`.

## What gets mirrored

A single AIFactory release tag (e.g. `v1.0.0`) is actually a small object graph in the registry:

| Artifact                          | OCI media type                                  | Purpose                                                |
| --------------------------------- | ----------------------------------------------- | ------------------------------------------------------ |
| Image manifest                    | `application/vnd.oci.image.manifest.v1+json`    | The container image itself (`sha256:<digest>`)         |
| Cosign signature                  | `application/vnd.dev.cosign.simplesigning.v1+json` | Sigstore-issued signature over the image digest        |
| SBOM attestation                  | `application/vnd.in-toto+json` (`spdx` predicate) | Software Bill of Materials                             |
| SLSA-3 provenance attestation     | `application/vnd.in-toto+json` (`slsaprovenance` predicate) | Build-process provenance                               |

`cosign copy` walks the full graph and re-uploads every object to the target registry while keeping the same `sha256:` digests, so verification against the upstream OIDC identity still succeeds at the mirror.

## Pre-flight: verify the upstream image

Before you mirror anything, prove the upstream artifact is what you think it is. If this step fails, do NOT mirror — investigate first.

```bash
export SOURCE_IMAGE="ghcr.io/olafkfreund/aifactory:v1.0.0"

cosign verify \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
    --certificate-identity-regexp "https://github.com/olafkfreund/AIFactory/.+" \
    "${SOURCE_IMAGE}"
```

A successful verify prints the verified signature payload as JSON and exits 0. Capture the printed `sha256:...` digest — every downstream step in this runbook re-references that exact digest.

## Mirror with `cosign copy`

The drill script `scripts/drills/image-mirroring.sh` automates the steps below; this section explains what it does so you can adapt it for non-standard registries.

### Generic recipe

```bash
export SOURCE_IMAGE="ghcr.io/olafkfreund/aifactory:v1.0.0"
export TARGET_REGISTRY="registry.internal.example.com/aifactory:v1.0.0"

cosign copy "${SOURCE_IMAGE}" "${TARGET_REGISTRY}"
```

`cosign copy` performs three operations in one call:

1. Fetches the source manifest + every referenced layer + every `.sig` / `.att` companion object.
2. Re-uploads them all to the target, preserving content-addressable digests.
3. Updates the tag at the target so `pull` works by tag (not just by digest).

The flag `--force` overwrites the tag at the destination; `--platform linux/amd64,linux/arm64` mirrors only the named platforms when the upstream is a multi-arch image index.

### Registry-specific examples

#### Harbor (on-prem, the most common air-gap target)

```bash
docker login harbor.internal.example.com
cosign copy \
    ghcr.io/olafkfreund/aifactory:v1.0.0 \
    harbor.internal.example.com/aifactory/web:v1.0.0
```

Make sure the Harbor project has **content trust** enabled if you intend to enforce signature-presence at pull time via the Harbor admission webhook. The cosign objects show up in the Harbor UI as `.sig` and `.att` tags alongside the image tag.

#### JFrog Artifactory

```bash
# Artifactory needs a docker repo with OCI v2 support enabled.
docker login example.jfrog.io
cosign copy \
    ghcr.io/olafkfreund/aifactory:v1.0.0 \
    example.jfrog.io/aifactory-docker/web:v1.0.0
```

Older Artifactory builds (pre-7.55) had buggy SHA256 manifest handling that corrupted `cosign` payloads. Upgrade to 7.63+ before mirroring signed images.

#### GitHub Container Registry (GHCR — internal org mirror)

```bash
echo "${GHCR_TOKEN}" | docker login ghcr.io -u <username> --password-stdin
cosign copy \
    ghcr.io/olafkfreund/aifactory:v1.0.0 \
    ghcr.io/your-org/aifactory:v1.0.0
```

Useful when you want to fork the build into your own GitHub org and re-sign with your own OIDC identity (see [Re-signing at the mirror](#re-signing-at-the-mirror) below).

#### AWS Elastic Container Registry (ECR)

```bash
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin \
    123456789012.dkr.ecr.us-east-1.amazonaws.com

aws ecr create-repository --repository-name aifactory/web --region us-east-1 || true

cosign copy \
    ghcr.io/olafkfreund/aifactory:v1.0.0 \
    123456789012.dkr.ecr.us-east-1.amazonaws.com/aifactory/web:v1.0.0
```

ECR requires the repository to exist before the first push (no auto-create on push). Pre-provision via Terraform or the inline `aws ecr create-repository` above.

#### Azure Container Registry (ACR)

```bash
az acr login --name yourorgacr
cosign copy \
    ghcr.io/olafkfreund/aifactory:v1.0.0 \
    yourorgacr.azurecr.io/aifactory/web:v1.0.0
```

#### Google Artifact Registry (GAR)

```bash
gcloud auth configure-docker us-docker.pkg.dev
cosign copy \
    ghcr.io/olafkfreund/aifactory:v1.0.0 \
    us-docker.pkg.dev/your-project/aifactory/web:v1.0.0
```

## Verification post-mirror

The whole point of `cosign copy` (rather than `pull && push`) is that the mirrored image still verifies against the upstream OIDC identity. Confirm this immediately after every mirror, and ideally schedule it as a recurring drill.

```bash
cosign verify \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
    --certificate-identity-regexp "https://github.com/olafkfreund/AIFactory/.+" \
    "registry.internal.example.com/aifactory:v1.0.0"
```

Compare digests to be doubly sure no bit-flip occurred during transit:

```bash
SRC=$(cosign triangulate ghcr.io/olafkfreund/aifactory:v1.0.0 \
        | grep -oP 'sha256:[a-f0-9]+' | head -1)
TGT=$(cosign triangulate registry.internal.example.com/aifactory:v1.0.0 \
        | grep -oP 'sha256:[a-f0-9]+' | head -1)
[ "$SRC" = "$TGT" ] && echo "OK: digests match ($SRC)" || echo "FAIL: $SRC vs $TGT"
```

The drill script `scripts/drills/image-mirroring.sh` automates pre-flight verify, copy, post-mirror verify, and digest equality in one invocation. Run `scripts/drills/image-mirroring.sh --help` for the env-var contract; run it `--dry-run` in CI to gate the procedure documentation against the script every commit.

## Re-signing at the mirror

Some regulators require the artifact running in production to be signed by the *operator's* OIDC identity, not the upstream maintainer. After mirroring, you can attach an additional cosign signature using your own OIDC issuer (your corporate Okta, Azure AD, internal Sigstore Fulcio):

```bash
COSIGN_EXPERIMENTAL=1 cosign sign \
    --oidc-issuer "https://login.example.com" \
    "registry.internal.example.com/aifactory@${TGT}"
```

This **adds** a signature; it does NOT remove the upstream one. Downstream admission controllers can be configured to accept EITHER signature (defence in depth) or REQUIRE the operator's signature only (full chain-of-custody to the operator org). The upstream signature remains as forensic evidence of provenance.

## Helm + Kubernetes wiring

Once the mirror is populated, point AIFactory's Helm chart at it via `values.yaml`:

```yaml
image:
  registry: registry.internal.example.com
  repository: aifactory
  tag: v1.0.0
  pullPolicy: IfNotPresent
imagePullSecrets:
  - name: regcred-internal
```

For air-gapped clusters that enforce signature-presence at admission (Sigstore policy-controller, Kyverno `verifyImages`, or the OPA Gatekeeper cosign template), add the upstream OIDC identity to the allowlist so the mirrored image's preserved signature still satisfies the policy.

## Troubleshooting

| Symptom                                                                                                        | Likely cause                                              | Fix                                                                                                |
| -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `cosign verify` works upstream but FAILS at the mirror.                                                        | Used `docker pull && push` instead of `cosign copy`.      | Re-mirror with `cosign copy`. Push the `.sig` tags too if you must use a custom transport.         |
| `cosign copy` succeeds but `cosign verify` at mirror says `no matching signatures`.                            | Registry stripped OCI artifact references during push.    | Upgrade the registry (Artifactory < 7.63, Harbor < 2.6, Nexus < 3.41 are known problematic).      |
| Digests differ between source and mirror.                                                                      | Registry re-compressed layers (illegal per OCI spec).     | Disable layer re-compression in registry config; re-run the drill.                                 |
| Pulls succeed but admission controller denies the pod with "signature not found".                              | Admission controller searches the WRONG OIDC issuer.      | Add `https://token.actions.githubusercontent.com` to the controller's allowlist, OR re-sign at mirror with the operator's issuer. |

## Related documentation

- `guides/deployment/runbook.md` — fresh-install procedure, includes a private-registry section.
- `guides/deployment/upgrade.md` — upgrade procedure between releases when running off an internal mirror.
- `docs/docs/concepts/multi-replica.md` — supply-chain story end-to-end (signing, attestation, verification at admission).
- `scripts/drills/image-mirroring.sh` — executable form of this runbook; CI runs `--dry-run` on every PR.
