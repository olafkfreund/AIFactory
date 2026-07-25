"""Push the access-review export to the MinIO evidence drop-path (Factory#324).

The evidence-collector CronJob (factory-gitops#74) snapshots what an
in-cluster job can *reach* into the ``factory-evidence`` bucket, and reserves
a stable drop-path for evidence the control plane must push *itself*:

    factory-evidence/control-plane-push/<source>/...

This job fills the ``access-review`` source: once per day it runs the same
access-review export that backs ``GET /api/admin/access-review`` — but
fleet-wide (every non-deleted org, one line per member, ``org_id`` on each
line) — and uploads the dated NDJSON to

    s3://factory-evidence/control-plane-push/access-review/<YYYY-MM-DD>.ndjson

so an assessor can trace the SOC2 CC6.2 / ISO 27001 A.9.2.5 quarterly
access-review requirement to live, dated evidence without an operator having
to hit the endpoint by hand.

Idempotent: re-running for the same UTC day overwrites that day's key.
Fail-safe: any upload/DB error logs WARNING and returns None — this is a
best-effort evidence push, never a hard dependency of a request path. The
next daily tick retries.

MinIO connection reuses the artifact-store env namespace the pods already
carry (apis/concurrency-conventions.md §2, see core/artifact_store.py):
``S3_ENDPOINT`` / ``S3_ACCESS_KEY`` / ``S3_SECRET_KEY`` / ``S3_REGION``.
The bucket is ``factory-evidence`` (override ``EVIDENCE_S3_BUCKET``).

Run once (Kubernetes CronJob / manual operator run):

    python -m server.jobs.access_review_evidence_cron
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import Organization, OrgMember, User
from ..routes.access_review import _member_line

logger = logging.getLogger(__name__)

# The evidence bucket + the drop-path prefix reserved by factory-gitops#74.
_EVIDENCE_BUCKET = os.environ.get("EVIDENCE_S3_BUCKET", "factory-evidence")
_DROP_PREFIX = "control-plane-push/access-review"

# Type of the pluggable uploader (real boto3 by default; a fake in tests).
Uploader = Callable[[str, bytes], None]


def evidence_key(day: date) -> str:
    """The dated object key under the reserved drop-path."""
    return f"{_DROP_PREFIX}/{day.isoformat()}.ndjson"


async def build_ndjson(db: AsyncSession) -> bytes:
    """Fleet-wide access-review snapshot as NDJSON bytes.

    One line per (OrgMember, User) across every non-deleted org, ordered by
    ``(org_id, email)`` for a stable, diffable export. Each line carries
    ``org_id`` (the endpoint omits it because the caller passes ``?org=``;
    the fleet file needs it to tell tenants apart) plus the shared
    ``_member_line`` columns.
    """
    stmt = (
        select(OrgMember, User)
        .join(User, OrgMember.user_id == User.id)
        .join(Organization, OrgMember.org_id == Organization.id)
        .where(Organization.deleted_at.is_(None))
        .order_by(OrgMember.org_id.asc(), User.email.asc())
    )
    result = await db.execute(stmt)
    lines: list[str] = []
    for member, user in result.all():
        line: dict[str, object] = {"org_id": member.org_id, **_member_line(member, user)}
        lines.append(json.dumps(line))
    return ("\n".join(lines) + "\n" if lines else "").encode("utf-8")


def _boto3_upload(key: str, data: bytes) -> None:
    """Upload ``data`` to ``s3://<evidence-bucket>/<key>`` via boto3.

    boto3 is imported lazily so importing this module (and running the pure
    export + key-layout tests) needs no third-party transport. The object is
    tagged ``role=evidence`` so the bucket's retention lifecycle — which
    filters on that tag (Factory#329, factory-gitops#67) — actually matches.
    """
    import boto3  # noqa: PLC0415 — lazy import keeps the module transport-free

    endpoint = os.environ.get("S3_ENDPOINT") or None
    if not endpoint:
        raise RuntimeError("S3_ENDPOINT is not set; cannot reach the evidence bucket")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY") or None,
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY") or None,
        region_name=os.environ.get("S3_REGION") or "us-east-1",
    )
    client.put_object(
        Bucket=_EVIDENCE_BUCKET,
        Key=key,
        Body=data,
        ContentType="application/x-ndjson",
        Tagging="role=evidence",
    )


async def push_access_review_evidence(
    db: AsyncSession,
    *,
    today: date | None = None,
    upload: Uploader = _boto3_upload,
) -> str | None:
    """Build the fleet access-review NDJSON and push it to the drop-path.

    Returns the ``s3://`` URI on success, or None on any failure (logged
    WARNING). ``today`` and ``upload`` are injected for testability.
    """
    day = today or datetime.now(timezone.utc).date()
    key = evidence_key(day)
    try:
        data = await build_ndjson(db)
        upload(key, data)
    except Exception:
        logger.warning(
            "access-review evidence: push failed for %s; will retry next tick",
            day,
            exc_info=True,
        )
        return None
    uri = f"s3://{_EVIDENCE_BUCKET}/{key}"
    logger.info(
        "access-review evidence: pushed %d bytes to %s", len(data), uri
    )
    return uri


def main() -> None:
    """`python -m server.jobs.access_review_evidence_cron` — push once."""
    import asyncio  # noqa: PLC0415 — CLI-entry-only

    from ..database.engine import async_session_factory

    logging.basicConfig(level=logging.INFO)

    async def _go() -> None:
        async with async_session_factory() as db:
            uri = await push_access_review_evidence(db)
            if uri is None:
                logger.warning("access-review evidence: nothing pushed (see warning above)")

    asyncio.run(_go())


if __name__ == "__main__":
    main()
