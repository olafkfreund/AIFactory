"""P0.6 — multi-arch manifest contains both amd64 and arm64."""

import json
import subprocess

import pytest


@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.skip(reason="P0.6 implementation pending: multi-arch build via docker buildx")
def test_multi_arch_manifest_exists(built_image: str) -> None:
    """`docker manifest inspect` reports both linux/amd64 and linux/arm64."""
    result = subprocess.run(
        ["docker", "manifest", "inspect", built_image],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"manifest inspect failed: {result.stderr}"
    manifest = json.loads(result.stdout)
    arches = {m["platform"]["architecture"] for m in manifest.get("manifests", [])}
    assert "amd64" in arches, f"amd64 not in manifest: {arches}"
    assert "arm64" in arches, f"arm64 not in manifest: {arches}"
