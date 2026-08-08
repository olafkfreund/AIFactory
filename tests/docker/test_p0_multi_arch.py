"""P0.6 — Dockerfile is multi-arch-capable (amd64 + arm64).

Design note: this test deliberately does NOT cross-build the full image
on every PR. Cross-arch builds via QEMU emulation take 10+ minutes
because every native Python wheel + npm install runs under emulation,
and the value on every PR is marginal — the real multi-arch artifact
emission happens in release.yml at tag push time, where 15 minutes is
acceptable.

What we DO verify here:
  1. `docker buildx` is available
  2. Every base image referenced in the Dockerfile is itself multi-arch
     (its manifest list contains both amd64 and arm64 entries)

That's the bank-grade contract: "this Dockerfile's base layers can be
resolved on both architectures." Combined with our policy that the
Dockerfile contains no arch-specific RUN steps (apk picks the right
arch package automatically), this proves multi-arch buildability
without paying the cross-emulation cost.
"""

import json
import re
import shutil
import subprocess

import pytest

from tests.docker.helpers import DOCKERFILE_PATH


def _extract_base_image_digests() -> list[str]:
    """Pull every external `FROM <image>` reference from the Dockerfile.

    Internal stage references (`FROM <previously-defined-stage>`) are skipped:
    they name an in-Dockerfile build stage, not a registry image, so
    `imagetools inspect` cannot resolve them.
    """
    text = DOCKERFILE_PATH.read_text()
    aliases: set[str] = set()
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 4 and parts[0].upper() == "FROM" and parts[2].upper() == "AS":
            aliases.add(parts[3].lower())

    refs: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("FROM "):
            continue
        # `FROM image:tag@sha256:abc AS stage` — capture up to the first
        # whitespace after the image reference.
        match = re.match(r"FROM\s+(\S+)", stripped, re.IGNORECASE)
        if match and match.group(1).lower() not in aliases:
            refs.append(match.group(1))
    return refs


# A cold `docker buildx version` has to start the daemon's buildx plugin, which
# on a loaded CI runner is not a 10-second operation (#1186). This probe only
# decides whether to skip, so a generous ceiling costs nothing and a tight one
# turned a skip into a red build.
_BUILDX_PROBE_TIMEOUT_S = 60


@pytest.mark.docker
@pytest.mark.slow
def test_multi_arch_buildable() -> None:
    """P0.6 — base images we pin support both linux/amd64 and linux/arm64.

    Proves multi-arch capability without cross-building. Fast (~2 s).

    Marked ``slow`` as well as ``docker`` (#1186). It was the only test under
    ``tests/docker/`` carrying just the one marker, so every ``-m "not slow"``
    lane collected it — including the nested ``pytest tests/ -m "not slow and
    not postgres"`` subprocess inside the postgres acceptance suite, which runs
    on a job with no Buildx setup step at all. A Docker-dependent test was
    gating a Postgres lane.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    try:
        bx = subprocess.run(
            ["docker", "buildx", "version"],
            capture_output=True,
            text=True,
            timeout=_BUILDX_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # A probe that decides whether to skip must not be able to fail the
        # suite. "The daemon did not answer" is the same fact as "buildx is not
        # available": the test's own precondition was not met, so it cannot run
        # — it has learned nothing about the Dockerfile either way.
        pytest.skip(
            f"docker buildx did not respond within {_BUILDX_PROBE_TIMEOUT_S}s "
            "— treating as unavailable"
        )
    if bx.returncode != 0:
        pytest.skip("docker buildx not installed")

    refs = _extract_base_image_digests()
    assert refs, "no FROM lines found in Dockerfile"

    for ref in refs:
        # `docker buildx imagetools inspect --raw` returns the image-index
        # manifest list as JSON. Multi-arch images have `manifests[]` with
        # one entry per platform.
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"`docker buildx imagetools inspect --raw {ref}` failed:\n"
            f"--- stderr ---\n{result.stderr[-1000:]}"
        )
        manifest = json.loads(result.stdout)
        arches = {
            entry.get("platform", {}).get("architecture")
            for entry in manifest.get("manifests", [])
        }
        assert "amd64" in arches, (
            f"{ref} does not include linux/amd64 (found arches: {sorted(a for a in arches if a)})"
        )
        assert "arm64" in arches, (
            f"{ref} does not include linux/arm64 (found arches: {sorted(a for a in arches if a)})"
        )
