"""P0.7 / P0.8 / P0.9 / P0.10 — supply-chain hardening:
digest pinning, Trivy scan, SBOM attestation, cosign signing."""

import json
import os
import subprocess

import pytest

from tests.docker.helpers import DOCKERFILE_PATH

IN_CI = os.environ.get("CI", "").lower() == "true"


@pytest.mark.docker
@pytest.mark.skip(reason="P0.7 implementation pending: pin base images by SHA-256 digest")
def test_base_images_pinned_by_digest() -> None:
    """P0.7 — every `FROM` line uses `@sha256:...`, not a floating tag."""
    content = DOCKERFILE_PATH.read_text()
    from_lines = [
        line.strip() for line in content.splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    assert from_lines, "no FROM lines found in Dockerfile.chainguard"
    for line in from_lines:
        assert "@sha256:" in line, \
            f"FROM line is not digest-pinned: {line!r}"


@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.skipif(not IN_CI, reason="Trivy scan enforced only in CI")
@pytest.mark.skip(reason="P0.8 implementation pending: Trivy scan with fail-on HIGH/CRITICAL")
def test_trivy_no_high_critical(built_image: str) -> None:
    """P0.8 — Trivy scan reports zero HIGH/CRITICAL vulnerabilities."""
    result = subprocess.run(
        ["trivy", "image", "--severity", "HIGH,CRITICAL", "--format", "json", built_image],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, f"trivy failed: {result.stderr}"
    report = json.loads(result.stdout)
    findings = []
    for target in report.get("Results", []) or []:
        findings.extend(target.get("Vulnerabilities", []) or [])
    assert not findings, (
        f"Trivy found {len(findings)} HIGH/CRITICAL vulns: "
        f"{[(v.get('VulnerabilityID'), v.get('Severity')) for v in findings[:5]]}"
    )


@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.skipif(not IN_CI, reason="SBOM attestation only published from CI release pipeline")
@pytest.mark.skip(reason="P0.9 implementation pending: Syft SBOM + cosign attestation")
def test_sbom_attested(built_image: str) -> None:
    """P0.9 — SPDX SBOM is attached as a cosign attestation."""
    result = subprocess.run(
        ["cosign", "download", "attestation", built_image],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"cosign download attestation failed: {result.stderr}"
    found_spdx = False
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        envelope = json.loads(line)
        if "spdx" in envelope.get("payloadType", "").lower():
            found_spdx = True
            break
    assert found_spdx, "no SPDX SBOM attestation found on the image"


@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.skipif(not IN_CI, reason="cosign keyless verify requires Sigstore + GitHub OIDC")
@pytest.mark.skip(reason="P0.10 implementation pending: cosign keyless signing via Sigstore")
def test_cosign_verifies(built_image: str) -> None:
    """P0.10 — image signature verifies against the expected GitHub OIDC identity."""
    result = subprocess.run(
        [
            "cosign", "verify",
            "--certificate-identity-regexp", r"^https://github\.com/olafkfreund/AIFactory/",
            "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
            built_image,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"cosign verify failed: {result.stderr}"
