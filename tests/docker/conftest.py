"""Pytest fixtures for P0 docker acceptance tests.

Tests are marked `@pytest.mark.docker` and `@pytest.mark.slow`. Default
CI (`-m "not slow"`) excludes them. The new `docker-acceptance` CI job
opts in with `-m docker`.

The `built_image` session fixture builds the Chainguard Dockerfile once
per test session and yields the image tag. Tests skip cleanly while
P0.1 is pending (Dockerfile.chainguard doesn't exist yet).
"""

from __future__ import annotations

import pytest

from tests.docker.helpers import (
    DOCKERFILE_PATH,
    docker_available,
    docker_build,
    docker_kill,
)

IMAGE_TAG = "aifactory:p0-test"


@pytest.fixture(scope="session")
def built_image() -> str:
    """Build the P0 image once per session; return its tag.

    Skips when Docker isn't available or the target Dockerfile doesn't
    exist yet — lets the harness land before P0.1 ships.
    """
    if not docker_available():
        pytest.skip("Docker not available on this host")
    if not DOCKERFILE_PATH.exists():
        pytest.skip(f"{DOCKERFILE_PATH.name} not present yet (P0.1 pending)")

    result = docker_build(DOCKERFILE_PATH, IMAGE_TAG)
    if result.returncode != 0:
        pytest.fail(
            f"docker build failed (exit {result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return IMAGE_TAG


@pytest.fixture
def container_name(request: pytest.FixtureRequest):
    """Per-test container name; cleaned up after the test."""
    name = f"aifactory-p0-test-{request.node.name}"
    yield name
    docker_kill(name)
