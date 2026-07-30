"""Access-review export endpoint (Epic #35 #43 PR-1b4).

Evidence for SOC2 CC6.2 + ISO 27001 A.9.2.5 quarterly access reviews.
Returns the current OrgMember roster with email, role,
``last_login_at`` (Epic #35 #43 column added in PR-1a), and active
flag.

## Honest scope (per design)

- **Current state only.** The endpoint returns the membership AS IT
  IS NOW. The original design proposed ``?as_of=YYYY-MM-DD``, but
  the OrgMember schema has no historical-membership table — we can't
  show membership-at-date without scope creep into a separate epic.
  Audit log queries for ``org.member.add/remove`` events provide
  the change history.

- **One org per call.** No multi-tenant access review in v1.1; the
  ``?org`` query parameter is required.

- **NDJSON output.** Matches the audit-export pattern so reviewers
  can stream into their existing audit pipelines.

## Auth

Requires **admin** or **owner** role in the target org (same
dependency as ``list_audit_logs``).
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.engine import get_db
from ..database.models import OrgMember, User
from .organizations import require_org_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Admin"])


@router.get(
    "/access-review",
    summary="Stream current org membership for an access review",
    description=(
        "Returns NDJSON: one line per current OrgMember of the "
        "specified org. Covers SOC2 CC6.2 + ISO 27001 A.9.2.5. "
        "Requires admin or owner role in the org. Current state only "
        "— use audit log queries for membership history."
    ),
)
async def export_access_review(
    org: str = Query(..., description="Organization ID to review"),
    membership: OrgMember = Depends(require_org_role("admin")),  # noqa: ARG001
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream the current OrgMember roster as NDJSON.

    Each line is one User+OrgMember pair with the columns required
    for a quarterly access review. Streaming so a 10k-member tenant
    doesn't materialise everything in memory.
    """
    return StreamingResponse(
        _stream_review(db, org_id=org),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": (
                f'attachment; filename="access-review-{org}.ndjson"'
            ),
        },
    )


def _member_line(member: OrgMember, user: User) -> dict[str, object]:
    """The access-review columns for one OrgMember+User pair.

    Extracted so the scheduled evidence push
    (``server.jobs.access_review_evidence_cron``) emits byte-identical
    lines to the ``/api/admin/access-review`` endpoint — one exporter,
    two callers.
    """
    return {
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "role": member.role,
        "active": user.is_active,
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "last_login_at": (
            user.last_login_at.isoformat() if user.last_login_at else None
        ),
    }


async def _stream_review(
    db: AsyncSession,
    *,
    org_id: str,
) -> AsyncIterator[bytes]:
    """Yield NDJSON lines, one per OrgMember+User."""
    stmt = (
        select(OrgMember, User)
        .join(User, OrgMember.user_id == User.id)
        .where(OrgMember.org_id == org_id)
        .order_by(User.email.asc())
    )
    result = await db.execute(stmt)
    for member, user in result.all():
        yield (json.dumps(_member_line(member, user)) + "\n").encode("utf-8")
