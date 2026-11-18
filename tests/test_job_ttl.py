"""Finished Job TTL is shared by all AIFactory Job builders (#2133)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "apps/web-server", _ROOT / "apps/backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from server.services import (  # noqa: E402
    agent_job_backend,
    build_backend,
    spec_job_backend,
)


@pytest.mark.parametrize(
    ("raw", "expected", "warns"),
    [
        (None, 300, False),
        ("600", 600, False),
        ("bad", 300, True),
        ("0", 300, True),
        ("-1", 300, True),
        ("3601", 3600, True),
    ],
)
def test_job_ttl_seconds(monkeypatch, caplog, raw, expected, warns):
    if raw is None:
        monkeypatch.delenv("AIFACTORY_JOB_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("AIFACTORY_JOB_TTL_SECONDS", raw)
    assert build_backend.job_ttl_seconds() == expected
    assert bool(caplog.records) is warns


@pytest.mark.parametrize(("raw", "expected"), [(None, 300), ("600", 600)])
def test_all_job_manifests_use_finished_ttl(monkeypatch, raw, expected, tmp_path):
    if raw is None:
        monkeypatch.delenv("AIFACTORY_JOB_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("AIFACTORY_JOB_TTL_SECONDS", raw)

    manifests = [
        build_backend.build_run_py_job_manifest(
            task_id="p:s", project_path=tmp_path, spec_id="s"
        ),
        spec_job_backend.build_spec_job_manifest(
            task_id="p:s",
            fetch_id="a" * 32,
            spec_id="s",
            workspace_uri="s3://bucket/workspace.tar.gz",
            extra_env={},
        ),
        agent_job_backend.build_agent_job_manifest(
            job_id="review-123",
            argv=["claude", "--print"],
            workspace_uri=None,
            extra_env={},
            models=["haiku"],
        ),
    ]
    assert [manifest["spec"]["ttlSecondsAfterFinished"] for manifest in manifests] == [
        expected,
    ] * 3
