"""Per-dispatch scoped S3 credentials for agent Jobs (#2023).

Flag ``AIFACTORY_SCOPED_JOB_S3`` (default off). On: the Job gets MinIO STS
credentials limited by a session policy to its own task's key prefixes, in place
of the control plane's bucket-wide ``S3_ACCESS_KEY``/``S3_SECRET_KEY``. The
vendored ``ArtifactStore`` passes ``None`` keys to boto3 when ``S3_ACCESS_KEY``
is unset, so boto3 picks the ``AWS_*`` session env up with no Job-side change.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

_FLAG = "AIFACTORY_SCOPED_JOB_S3"
_STS_KEY = "S3_STS_ACCESS_KEY"
_STS_SECRET = "S3_STS_SECRET_KEY"  # noqa: S105 - env var name, not a secret
_BAD_ID = set('*?/"\\')
_DEADLINE_ENV = "AIFACTORY_BUILD_DEADLINE_SECONDS"


def enabled() -> bool:
    return os.environ.get(_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def session_policy(bucket: str, prefixes: Iterable[str]) -> str:
    """The inline session policy: Get/Put on each prefix, nothing else (no List)."""
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"],
                    "Resource": [f"arn:aws:s3:::{bucket}/{p}/*" for p in prefixes],
                }
            ],
        },
        separators=(",", ":"),
    )


def _prefix(corr: str | None, job_id: str) -> str:
    from core.artifact_store import ArtifactRef  # noqa: PLC0415

    for part in (corr or "", job_id):
        if _BAD_ID & set(part):  # a wildcard in an id would widen the policy
            raise ValueError("unsafe S3 scope id")
    if not job_id.strip():
        raise ValueError("empty S3 scope id")
    return ArtifactRef(
        service="aifactory", job_id=job_id, role="build", correlation_key=corr
    ).prefix()


def _lifetime() -> int:
    """Longer of the build and spec Job deadlines, plus 1h Pending slack (seconds)."""
    try:
        deadline = int(os.environ.get(_DEADLINE_ENV) or 6 * 3600)
    except ValueError:
        deadline = 6 * 3600
    return max(deadline, 7200) + 3600  # 7200 = the spec Job deadline


def _assume(policy: str) -> dict[str, Any]:
    import boto3  # noqa: PLC0415

    sts = boto3.client(
        "sts",
        endpoint_url=os.environ["S3_ENDPOINT"],
        aws_access_key_id=os.environ[_STS_KEY],
        aws_secret_access_key=os.environ[_STS_SECRET],
        region_name=os.environ.get("S3_REGION") or "us-east-1",
    )
    # MinIO ignores RoleArn; boto3 only wants a well-formed one.
    out: dict[str, Any] = sts.assume_role(
        RoleArn="arn:aws:iam::minio:role/aifactory-job",
        RoleSessionName="aifactory-job",
        Policy=policy,
        DurationSeconds=_lifetime(),
    )["Credentials"]
    return out


def scope_job_s3(env: dict[str, str], ids: Iterable[tuple[str | None, str]]) -> None:
    """Swap the bucket-wide S3 keys in ``env`` for STS credentials scoped to
    ``ids`` = ``(correlation_key, job_id)`` pairs. No-op with the flag off.
    Fails closed: raises rather than leaving the root key in the Job env.
    Blocking (network): call from a thread."""
    if not enabled():
        return
    from .build_backend import BuildBackendUnavailableError  # noqa: PLC0415

    env.pop("S3_SECRET_KEY", None)
    if not env.pop("S3_ACCESS_KEY", None):
        return  # no S3 configured: nothing to scope
    try:
        bucket = (
            env.get("S3_BUCKET") or os.environ.get("S3_BUCKET") or "factory-artifacts"
        )
        prefixes = sorted({_prefix(c, j) for c, j in ids})
        cred = _assume(session_policy(bucket, prefixes))
        env.update(
            AWS_ACCESS_KEY_ID=cred["AccessKeyId"],
            AWS_SECRET_ACCESS_KEY=cred["SecretAccessKey"],
            AWS_SESSION_TOKEN=cred["SessionToken"],
        )
    except Exception as exc:  # noqa: BLE001 - any STS failure must fail closed
        raise BuildBackendUnavailableError(
            f"scoped Job S3 credentials unavailable ({type(exc).__name__})"
        ) from None
