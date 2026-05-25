"""P0.6 — Dockerfile builds for both linux/amd64 and linux/arm64."""

import os
import shutil
import subprocess

import pytest

from tests.docker.helpers import DOCKERFILE_PATH, REPO_ROOT

IN_CI = os.environ.get("CI", "").lower() == "true"


@pytest.mark.docker
@pytest.mark.slow
@pytest.mark.skipif(
    not IN_CI,
    reason="Multi-arch build needs QEMU/binfmt; only enforced in CI (uses docker/setup-qemu-action)",
)
def test_multi_arch_buildable() -> None:
    """P0.6 — Dockerfile cross-builds successfully for amd64 + arm64.

    Uses `docker buildx build --output type=cacheonly` to verify both
    architectures compile end-to-end without producing manifest artifacts
    (saves CI time and bandwidth). The actual multi-arch image emission
    happens in release.yml at tag push time.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    bx = subprocess.run(
        ["docker", "buildx", "version"],
        capture_output=True, text=True, timeout=10,
    )
    if bx.returncode != 0:
        pytest.skip("docker buildx not installed")

    result = subprocess.run(
        [
            "docker", "buildx", "build",
            "--platform", "linux/amd64,linux/arm64",
            "-f", str(DOCKERFILE_PATH),
            "--output", "type=cacheonly",
            str(REPO_ROOT),
        ],
        capture_output=True, text=True, timeout=1800,
    )
    assert result.returncode == 0, (
        f"multi-arch build failed (exit {result.returncode}):\n"
        f"--- stderr (last 2000 chars) ---\n{result.stderr[-2000:]}"
    )
