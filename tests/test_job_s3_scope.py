"""Scoped per-dispatch S3 credentials for agent Jobs (#2023)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _path in (_REPO / "apps/web-server", _REPO / "apps/backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from server.services import job_s3_scope as scope  # noqa: E402
from server.services.build_backend import BuildBackendUnavailableError  # noqa: E402


def _env() -> dict[str, str]:
    return {"S3_ACCESS_KEY": "root", "S3_SECRET_KEY": "rootsecret", "S3_BUCKET": "bkt"}


@pytest.fixture
def sts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(policy: str) -> dict[str, str]:
        seen.append(policy)
        return {"AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t"}

    monkeypatch.setattr(scope, "_assume", fake)
    monkeypatch.setenv("AIFACTORY_SCOPED_JOB_S3", "true")
    return seen


def test_flag_off_leaves_env_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFACTORY_SCOPED_JOB_S3", raising=False)
    env = _env()
    scope.scope_job_s3(env, [(None, "t1")])
    assert env == _env()


def test_swaps_root_key_for_session_scoped_to_prefixes(sts: list[str]) -> None:
    env = _env()
    scope.scope_job_s3(env, [("42", "task-1"), (None, "spec-1"), (None, "spec-1")])
    assert "S3_ACCESS_KEY" not in env and "S3_SECRET_KEY" not in env
    assert env["AWS_ACCESS_KEY_ID"] == "a"
    assert env["AWS_SESSION_TOKEN"] == "t"
    stmt = json.loads(sts[0])["Statement"][0]
    assert stmt["Resource"] == [
        "arn:aws:s3:::bkt/aifactory/42/task-1/*",
        "arn:aws:s3:::bkt/aifactory/_/spec-1/*",
    ]
    assert "s3:ListBucket" not in stmt["Action"]
    assert "s3:PutObjectTagging" in stmt["Action"]


@pytest.mark.parametrize("bad", ["a*", "a/b", "", "a?", 'a"b'])
def test_unsafe_id_fails_closed_without_root_key(sts: list[str], bad: str) -> None:
    env = _env()
    with pytest.raises(BuildBackendUnavailableError):
        scope.scope_job_s3(env, [(None, bad)])
    assert "S3_ACCESS_KEY" not in env and not sts


def test_sts_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_SCOPED_JOB_S3", "1")

    def boom(_policy: str) -> dict[str, str]:
        raise KeyError("S3_STS_ACCESS_KEY")

    monkeypatch.setattr(scope, "_assume", boom)
    env = _env()
    with pytest.raises(BuildBackendUnavailableError):
        scope.scope_job_s3(env, [(None, "t1")])
    assert "S3_SECRET_KEY" not in env


def test_no_s3_configured_is_noop(sts: list[str]) -> None:
    env = {"FOO": "1"}
    scope.scope_job_s3(env, [(None, "t1")])
    assert env == {"FOO": "1"} and not sts


def test_assume_calls_sts_with_caller_keys_policy_and_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeSts:
        def assume_role(self, **kw: object) -> dict[str, dict[str, str]]:
            calls["assume"] = kw
            return {"Credentials": {"AccessKeyId": "a"}}

    boto3 = pytest.importorskip("boto3")
    monkeypatch.setattr(
        boto3, "client", lambda name, **kw: calls.update(name=name, **kw) or FakeSts()
    )
    monkeypatch.setenv("S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("S3_STS_ACCESS_KEY", "sts-user")
    monkeypatch.setenv("S3_STS_SECRET_KEY", "sts-pass")
    monkeypatch.setenv("AIFACTORY_BUILD_DEADLINE_SECONDS", "1800")
    scope._assume("{}")
    assert calls["name"] == "sts"
    assert calls["endpoint_url"] == "http://minio:9000"
    assert calls["aws_access_key_id"] == "sts-user"
    assert calls["assume"]["Policy"] == "{}"  # type: ignore[index]
    # A short build deadline must not cut a 2h spec Job off: max(1800, 7200) + 1h.
    assert calls["assume"]["DurationSeconds"] == 7200 + 3600  # type: ignore[index]
