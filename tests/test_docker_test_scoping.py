#!/usr/bin/env python3
"""A Docker-dependent test must not be reachable from a non-Docker lane (#1186).

`postgres (P1 acceptance, PG 15)` went red on
`tests/docker/test_p0_multi_arch.py::test_multi_arch_buildable` with
`subprocess.TimeoutExpired: Command '['docker','buildx','version']' timed out
after 10 seconds` — nothing to do with Postgres, or with the PR. PG 16 passed
the same commit and the dedicated `docker (P0 acceptance)` job passed too.

Reachability: `tests/postgres/test_p1_suite_against_postgres.py` shells out to
`pytest tests/ -m "not slow and not postgres"`. `test_multi_arch_buildable` was
the only test under `tests/docker/` carrying `@pytest.mark.docker` WITHOUT
`@pytest.mark.slow`, so that expression collected it — on a runner with no
Buildx setup step.

This asserts the real marker expression against the real collector rather than
the decorator, because the decorator is not what CI evaluates.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# Verbatim from tests/postgres/test_p1_suite_against_postgres.py and from
# ci.yml's `backend (ruff + pytest)` step. If either changes, this changes.
NESTED_POSTGRES_EXPR = "not slow and not postgres"
DEFAULT_LANE_EXPR = "not slow"


def _collect(marker_expr: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/docker/",
            "-m",
            marker_expr,
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout + result.stderr


@pytest.mark.parametrize(
    "marker_expr",
    [NESTED_POSTGRES_EXPR, DEFAULT_LANE_EXPR],
    ids=["postgres", "default"],
)
def test_no_docker_test_is_collected_by_a_non_docker_lane(marker_expr: str):
    collected = _collect(marker_expr)

    assert "test_multi_arch_buildable" not in collected, (
        f"a Docker-dependent test is collected by `-m {marker_expr!r}`; it will "
        "run on a job with no Docker/Buildx setup and fail for reasons that have "
        "nothing to do with that job"
    )


def test_the_docker_lane_still_collects_it():
    """Mutation check: deselecting it everywhere would be no fix at all."""
    assert "test_multi_arch_buildable" in _collect("docker")


def test_a_timed_out_probe_skips_rather_than_fails(monkeypatch, tmp_path):
    """A probe that decides whether to skip must not be able to fail the suite.

    The 10s ceiling turned a slow daemon into a hard failure instead of the skip
    the probe exists to produce.
    """
    import tests.docker.test_p0_multi_arch as mod

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", "buildx", "version"], timeout=60)

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(mod.subprocess, "run", _timeout)

    with pytest.raises(pytest.skip.Exception, match="did not respond"):
        mod.test_multi_arch_buildable()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
