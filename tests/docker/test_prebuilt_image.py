"""Unit tests for the P0 image build/reuse switch; no Docker daemon required."""

import subprocess

import pytest

from tests.docker import helpers


def test_reuses_prebuilt_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_P0_PREBUILT_IMAGE", "1")
    monkeypatch.setattr(helpers, "image_exists", lambda tag: True)
    monkeypatch.setattr(
        helpers, "docker_build", lambda *args, **kwargs: pytest.fail("built image")
    )

    assert (
        helpers.build_or_reuse(helpers.DOCKERFILE_PATH, "aifactory:p0-test", "runtime")
        is None
    )


def test_missing_prebuilt_image_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_P0_PREBUILT_IMAGE", "1")
    monkeypatch.setattr(helpers, "image_exists", lambda tag: False)
    monkeypatch.setattr(
        helpers, "docker_build", lambda *args, **kwargs: pytest.fail("built image")
    )

    with pytest.raises(RuntimeError, match="aifactory:p0-test"):
        helpers.build_or_reuse(helpers.DOCKERFILE_PATH, "aifactory:p0-test", "runtime")


def test_builds_when_prebuilt_flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFACTORY_P0_PREBUILT_IMAGE", raising=False)
    monkeypatch.setattr(
        helpers, "image_exists", lambda tag: pytest.fail("inspected image")
    )
    result = subprocess.CompletedProcess(args=[], returncode=0)
    monkeypatch.setattr(helpers, "docker_build", lambda dockerfile, tag, target: result)

    assert (
        helpers.build_or_reuse(helpers.DOCKERFILE_PATH, "aifactory:p0-test", "runtime")
        is result
    )
