"""SCIM 2.0 CRUD routes — Users + Groups (Epic #35 #41 PR-1b3).

Implements design decision #5 from docs/plans/2026-05-28-saml-scim-design.md.

Endpoints
---------

Users:
  POST   /scim/v2/Users
  GET    /scim/v2/Users           (filter + paging per RFC 7644 §3.4.2)
  GET    /scim/v2/Users/{id}
  PATCH  /scim/v2/Users/{id}      (RFC 7644 §3.5.2 — Azure AD-compat)
  PUT    /scim/v2/Users/{id}
  DELETE /scim/v2/Users/{id}      (soft-delete)

Groups (parallel table — not mapped to orgs for v1.1):
  POST   /scim/v2/Groups
  GET    /scim/v2/Groups
  GET    /scim/v2/Groups/{id}
  PATCH  /scim/v2/Groups/{id}
  PUT    /scim/v2/Groups/{id}
  DELETE /scim/v2/Groups/{id}     (soft-delete)

Security
--------

Every route uses ``Depends(require_scim_bearer)`` (constant-time
compare). The ``If-Match`` header is accepted but never enforced —
last-writer-wins per decision #5 (Okta retries with full PUT on 412,
which can clobber unrelated fields).

Soft-delete + 404-on-GET pattern
---------------------------------

Azure AD re-GETs after DELETE and expects 404. We return 404 when
``active=false`` even though the row still exists in the DB for audit.
The row is reachable only via admin tooling.

PATCH semantics (RFC 7644 §3.5.2.1)
-------------------------------------

``op: add`` on a multi-valued attribute (Group ``members``) MUST
append, NOT replace. Add-as-replace is the most common SCIM bug; Azure
AD relies on append semantics for incremental Group member sync.

Azure AD sends capitalised ops (``Add``, ``Replace``, ``Remove``). The
ScimPatchOperation model already accepts both casings; we normalise to
lowercase before dispatch.

Identity key constraint
-----------------------

scim.userName == saml.NameID == users.email

POST /scim/v2/Users rejects with 400 when ``userName != emails[0].value``
(decision #6). The email is stored in ``users.email`` and userName is
derived from it on every read — we do NOT store userName separately.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.engine import get_db
from ..database.models import ScimGroup, ScimGroupMember, User
from .auth import require_scim_bearer
from .filters import ScimFilterInvalid
from .filters import parse as parse_filter
from .schemas import (
    ScimEmail,
    ScimError,
    ScimListResponse,
    ScimMeta,
    ScimName,
    ScimPatchRequest,
    ScimUser,
)
from .schemas import (
    ScimGroup as ScimGroupSchema,
)
from .schemas import (
    ScimGroupMember as ScimGroupMemberSchema,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scim/v2", tags=["SCIM"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCIM_CONTENT_TYPE = "application/scim+json"


def _scim_error(
    http_status: int, detail: str, scim_type: str | None = None
) -> JSONResponse:
    """Build a RFC 7644 §3.12 error response.

    We never echo attacker-controlled inputs in the ``detail`` field —
    callers pass a static string; any user-supplied value is logged
    separately at WARNING level.
    """
    body = ScimError(
        status=str(http_status),
        detail=detail,
        scim_type=scim_type,
    ).model_dump(by_alias=True, exclude_none=True)
    return JSONResponse(
        content=body, status_code=http_status, media_type=_SCIM_CONTENT_TYPE
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _user_to_scim(user: User, request: Request) -> dict[str, Any]:
    """Serialise a ``User`` ORM row into a SCIM User dict (wire format)."""
    given_name: str | None = None
    family_name: str | None = None
    if user.name:
        parts = user.name.split(" ", 1)
        given_name = parts[0]
        family_name = parts[1] if len(parts) > 1 else None

    resource = ScimUser(
        id=user.id,
        user_name=user.email or "",
        external_id=None,
        name=ScimName(
            given_name=given_name,
            family_name=family_name,
            formatted=user.name,
        )
        if user.name
        else None,
        display_name=user.name,
        emails=[ScimEmail(value=user.email or "", primary=True, type="work")]
        if user.email
        else [],
        active=user.is_active,
        meta=ScimMeta(
            resource_type="User",
            created=user.created_at.isoformat(timespec="seconds")
            if user.created_at
            else None,
            last_modified=user.updated_at.isoformat(timespec="seconds")
            if user.updated_at
            else None,
            location=str(request.url_for("scim_get_user", user_id=user.id)),
        ),
    )
    return resource.model_dump(by_alias=True, exclude_none=True)


def _group_to_scim(group: ScimGroup, request: Request) -> dict[str, Any]:
    """Serialise a ``ScimGroup`` ORM row into a SCIM Group dict (wire format)."""
    members = [
        ScimGroupMemberSchema(value=m.user_id, display=m.display, type="User")
        for m in (group.members or [])
    ]
    resource = ScimGroupSchema(
        id=group.id,
        external_id=group.external_id,
        display_name=group.display_name,
        members=members,
        meta=ScimMeta(
            resource_type="Group",
            created=group.created_at.isoformat(timespec="seconds")
            if group.created_at
            else None,
            last_modified=group.updated_at.isoformat(timespec="seconds")
            if group.updated_at
            else None,
            location=str(request.url_for("scim_get_group", group_id=group.id)),
        ),
    )
    return resource.model_dump(by_alias=True, exclude_none=True)


def _apply_user_patch(user: User, patch: ScimPatchRequest) -> None:
    """Mutate ``user`` in-place per the PATCH operations.

    Handles Azure AD's capitalised ops (``Add``, ``Replace``, ``Remove``).
    For multi-valued attributes the ``add`` op MUST append (RFC 7644
    §3.5.2.1). For User all attributes are scalar so add == replace in
    practice, but we implement the semantics correctly for consistency.
    """
    for op in patch.operations:
        op_lower = op.op.lower()
        path = (op.path or "").lower().strip()

        if op_lower in ("add", "replace"):
            if path in ("active",) or path == "":
                # Whole-resource PATCH without path — check for active in value dict
                if isinstance(op.value, dict):
                    if "active" in op.value:
                        user.is_active = bool(op.value["active"])
                    if "displayName" in op.value:
                        user.name = op.value["displayName"]
                    if "name" in op.value and isinstance(op.value["name"], dict):
                        name_parts = []
                        given = op.value["name"].get("givenName", "")
                        family = op.value["name"].get("familyName", "")
                        if given:
                            name_parts.append(given)
                        if family:
                            name_parts.append(family)
                        if name_parts:
                            user.name = " ".join(name_parts)
                elif path == "active":
                    user.is_active = bool(op.value)
            elif path == "displayname":
                user.name = str(op.value) if op.value is not None else None
            elif path in ("name.givenname", "name.familyname"):
                # Partial name update — update the stored name best-effort
                # by merging with the current stored value.
                current_parts = (user.name or "").split(" ", 1)
                if path == "name.givenname":
                    given = str(op.value) if op.value is not None else ""
                    family = current_parts[1] if len(current_parts) > 1 else ""
                else:
                    given = current_parts[0] if current_parts else ""
                    family = str(op.value) if op.value is not None else ""
                user.name = " ".join(filter(None, [given, family])) or None
            elif path == "username":
                # userName == email hard constraint; updating userName
                # updates the email. The route layer already validated
                # the constraint on POST — PATCH can update consistently.
                user.email = str(op.value) if op.value is not None else None
            # Silently ignore unknown paths — RFC 7644 says to return 400
            # for unknown paths but pragmatically Azure AD sends paths we
            # don't model; 400 breaks the sync. Log for ops visibility.
            else:
                logger.info("SCIM PATCH User: ignoring unknown path %r", path)

        elif op_lower == "remove":
            if path == "active":
                user.is_active = False
            elif path in ("displayname", "name"):
                user.name = None
            else:
                logger.info("SCIM PATCH User: ignoring unknown remove path %r", path)


def _apply_group_patch(
    group: ScimGroup, patch: ScimPatchRequest, db: AsyncSession
) -> list[dict]:
    """Return a list of member mutation dicts: {"action": add|remove, "user_id": ..., "display": ...}

    Callers apply these against the DB session. Returns the list so the
    route handler can execute them after the sync flush.

    APPEND semantics for ``add`` on ``members`` is critical: RFC 7644
    §3.5.2.1 requires that ``op: add`` extends the list, not replaces it.
    """
    member_ops: list[dict] = []

    for op in patch.operations:
        op_lower = op.op.lower()
        path = (op.path or "").lower().strip()

        if op_lower in ("add", "replace"):
            if path in ("", "members"):
                # Whole-resource or explicit members path.
                member_list = op.value if isinstance(op.value, list) else []
                if isinstance(op.value, dict):
                    member_list = op.value.get("members", [])
                for m in member_list:
                    user_id = m.get("value", "") if isinstance(m, dict) else str(m)
                    display = m.get("display") if isinstance(m, dict) else None
                    if user_id:
                        member_ops.append(
                            {"action": "add", "user_id": user_id, "display": display}
                        )
            elif path == "displayname":
                group.display_name = (
                    str(op.value) if op.value is not None else group.display_name
                )
            else:
                logger.info("SCIM PATCH Group: ignoring unknown path %r", path)

        elif op_lower == "remove":
            if path in ("", "members"):
                member_list = op.value if isinstance(op.value, list) else []
                for m in member_list:
                    user_id = m.get("value", "") if isinstance(m, dict) else str(m)
                    if user_id:
                        member_ops.append(
                            {"action": "remove", "user_id": user_id, "display": None}
                        )
            elif path.startswith("members[value eq "):
                # Path filter syntax: members[value eq "<user_id>"]
                raw = path[len("members[value eq ") :]
                user_id = raw.rstrip("]").strip('"').strip("'")
                if user_id:
                    member_ops.append(
                        {"action": "remove", "user_id": user_id, "display": None}
                    )
            else:
                logger.info("SCIM PATCH Group: ignoring unknown remove path %r", path)

    return member_ops


# ---------------------------------------------------------------------------
# Users: POST
# ---------------------------------------------------------------------------


@router.post(
    "/Users",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
)
async def scim_create_user(
    body: ScimUser,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Create a SCIM User. 409 on email collision. 400 if userName != emails[0].value."""
    # Hard constraint: userName must equal the primary email address
    # (design decision #6). Operators who want decoupled userName wait for v1.2.
    primary_email = body.emails[0].value if body.emails else None
    if primary_email and body.user_name != primary_email:
        logger.warning("SCIM create user: userName/email mismatch (not echoed)")
        return _scim_error(
            400,
            "userName must equal emails[0].value (SCIM identity key constraint)",
            "invalidValue",
        )

    email = body.user_name  # userName == email
    if not email:
        return _scim_error(400, "userName is required", "invalidValue")

    # Check for existing user (case-insensitive per design doc).
    existing = await db.scalar(select(User).where(User.email == email.lower()))
    if existing is not None:
        logger.warning("SCIM create user: email collision (not echoed)")
        return _scim_error(409, "User with this email already exists", "uniqueness")

    # Derive display name from name complex attribute or displayName.
    display_name = body.display_name
    if not display_name and body.name:
        parts = [body.name.given_name, body.name.family_name]
        display_name = " ".join(p for p in parts if p) or None

    user = User(
        email=email.lower(),
        name=display_name,
        # Users provisioned via SCIM have no password; the portal auth
        # layer will gate them via SAML/OIDC. An empty hash is never
        # accepted by the password login path.
        password_hash="scim-provisioned",
        is_active=body.active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    resource = _user_to_scim(user, request)
    return JSONResponse(
        content=resource,
        status_code=status.HTTP_201_CREATED,
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Users: GET list
# ---------------------------------------------------------------------------


@router.get(
    "/Users",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
    name="scim_list_users",
)
async def scim_list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
    filter: str | None = None,
    # RFC 7644 uses camelCase query params; FastAPI aliases let us accept both.
    startIndex: int = 1,
    count: int = 100,
) -> JSONResponse:
    """List Users with optional filter + paging.

    Soft-deleted users (``is_active=false``) are excluded by default.
    They are included ONLY when the filter is ``active eq false``.
    This matches Azure AD's sync pattern: it lists all active users
    during normal sync; listing inactive users is an explicit audit action.
    """
    scim_filter = None

    if filter:
        try:
            scim_filter = parse_filter(filter)
        except ScimFilterInvalid as exc:
            logger.info("SCIM list users: invalid filter (not echoed): %s", exc)
            return _scim_error(400, "Invalid filter syntax", "invalidFilter")

    # Build base query — only active users unless specifically filtering on active.
    stmt = select(User)

    if scim_filter:
        if scim_filter.attribute == "active":
            # When filtering on active, respect the value directly.
            stmt = stmt.where(User.is_active == scim_filter.value)
        elif scim_filter.attribute == "userName":
            stmt = stmt.where(User.email == str(scim_filter.value).lower())
            stmt = stmt.where(User.is_active == True)  # noqa: E712 — SQLAlchemy requires == True
        elif scim_filter.attribute == "externalId":
            # externalId is not stored directly — it would require joining
            # external_identities. For v1.1 we return an empty list for
            # externalId filters on Users (Groups have a dedicated column).
            # Okta uses externalId on Groups, not Users. Log for visibility.
            logger.info(
                "SCIM list users: externalId filter on Users not supported; returning empty"
            )
            list_resp = ScimListResponse(
                total_results=0,
                start_index=startIndex,
                items_per_page=0,
                resources=[],
            )
            return JSONResponse(
                content=list_resp.model_dump(by_alias=True),
                media_type=_SCIM_CONTENT_TYPE,
            )
    else:
        # Default: active users only.
        stmt = stmt.where(User.is_active == True)  # noqa: E712

    # Count before paging (RFC 7644 §3.4.2 requires totalResults of the
    # full matching set, not just the page).
    from sqlalchemy import func as sqlfunc

    count_stmt = select(sqlfunc.count()).select_from(stmt.subquery())
    total = await db.scalar(count_stmt) or 0

    # Apply paging.  startIndex is 1-based per RFC 7644.
    offset = max(0, startIndex - 1)
    page_stmt = stmt.offset(offset).limit(count)
    rows = (await db.execute(page_stmt)).scalars().all()

    resources = [_user_to_scim(u, request) for u in rows]
    list_resp = ScimListResponse(
        total_results=total,
        start_index=startIndex,
        items_per_page=len(resources),
        resources=resources,
    )
    return JSONResponse(
        content=list_resp.model_dump(by_alias=True),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Users: GET single
# ---------------------------------------------------------------------------


@router.get(
    "/Users/{user_id}",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
    name="scim_get_user",
)
async def scim_get_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Return a single User. 404 if missing OR soft-deleted.

    Returning 404 on soft-deleted rows is intentional: Azure AD
    re-GETs after DELETE and expects 404 to confirm the deprovisioning
    completed. Returning 200 with active=false would cause a sync loop.
    """
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return _scim_error(404, "User not found")

    return JSONResponse(
        content=_user_to_scim(user, request),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Users: PATCH
# ---------------------------------------------------------------------------


@router.patch(
    "/Users/{user_id}",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
)
async def scim_patch_user(
    user_id: str,
    body: ScimPatchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Per-attribute update per RFC 7644 §3.5.2.

    Azure AD sends capitalised ops (``Add``, ``Replace``, ``Remove``).
    The ScimPatchOperation model accepts both; we normalise to lowercase
    before dispatch in _apply_user_patch.
    """
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return _scim_error(404, "User not found")

    try:
        _apply_user_patch(user, body)
    except Exception as exc:
        logger.warning("SCIM PATCH user %s: apply error: %s", user_id, exc)
        return _scim_error(400, "Could not apply PATCH operations", "invalidSyntax")

    await db.commit()
    await db.refresh(user)
    return JSONResponse(
        content=_user_to_scim(user, request),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Users: PUT
# ---------------------------------------------------------------------------


@router.put(
    "/Users/{user_id}",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
)
async def scim_replace_user(
    user_id: str,
    body: ScimUser,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Whole-resource replace. 404 if missing or soft-deleted."""
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return _scim_error(404, "User not found")

    # Validate userName == email constraint on PUT as well.
    primary_email = body.emails[0].value if body.emails else None
    if primary_email and body.user_name != primary_email:
        return _scim_error(
            400,
            "userName must equal emails[0].value (SCIM identity key constraint)",
            "invalidValue",
        )

    display_name = body.display_name
    if not display_name and body.name:
        parts = [body.name.given_name, body.name.family_name]
        display_name = " ".join(p for p in parts if p) or None

    user.email = body.user_name.lower()
    user.name = display_name
    user.is_active = body.active

    await db.commit()
    await db.refresh(user)
    return JSONResponse(
        content=_user_to_scim(user, request),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Users: DELETE (soft)
# ---------------------------------------------------------------------------


@router.delete(
    "/Users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_scim_bearer)],
)
async def scim_delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> None:
    """Soft-delete: sets ``is_active=False``. Preserves row for audit.

    A subsequent GET on this user returns 404, matching Azure AD's
    expectation that a deprovisioned user is gone from SCIM's perspective.
    """
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ScimError(status="404", detail="User not found").model_dump(
                by_alias=True, exclude_none=True
            ),
        )
    if not user.is_active:
        # Already soft-deleted — idempotent 204.
        return

    user.is_active = False
    await db.commit()


# ---------------------------------------------------------------------------
# Groups: POST
# ---------------------------------------------------------------------------


@router.post(
    "/Groups",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
)
async def scim_create_group(
    body: ScimGroupSchema,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Create a SCIM Group. 409 if externalId already exists."""
    if body.external_id:
        dup = await db.scalar(
            select(ScimGroup).where(ScimGroup.external_id == body.external_id)
        )
        if dup is not None:
            return _scim_error(
                409, "Group with this externalId already exists", "uniqueness"
            )

    group = ScimGroup(
        display_name=body.display_name,
        external_id=body.external_id,
        active=True,
    )
    db.add(group)
    await db.flush()  # Populate group.id before inserting members.

    for m in body.members:
        member = ScimGroupMember(
            group_id=group.id,
            user_id=m.value,
            display=m.display,
        )
        db.add(member)

    await db.commit()
    # Re-fetch with members loaded; db.refresh() doesn't populate
    # selectin relationships, so we re-query explicitly.
    from sqlalchemy.orm import selectinload as _sil

    group = await db.scalar(
        select(ScimGroup)
        .options(_sil(ScimGroup.members))
        .where(ScimGroup.id == group.id)
    )
    return JSONResponse(
        content=_group_to_scim(group, request),
        status_code=status.HTTP_201_CREATED,
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Groups: GET list
# ---------------------------------------------------------------------------


@router.get(
    "/Groups",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
    name="scim_list_groups",
)
async def scim_list_groups(
    request: Request,
    db: AsyncSession = Depends(get_db),
    filter: str | None = None,
    startIndex: int = 1,
    count: int = 100,
) -> JSONResponse:
    """List Groups with optional filter + paging."""
    from sqlalchemy.orm import selectinload

    stmt = select(ScimGroup).options(selectinload(ScimGroup.members))
    scim_filter = None

    if filter:
        try:
            scim_filter = parse_filter(filter)
        except ScimFilterInvalid as exc:
            logger.info("SCIM list groups: invalid filter: %s", exc)
            return _scim_error(400, "Invalid filter syntax", "invalidFilter")

    if scim_filter:
        if scim_filter.attribute == "active":
            stmt = stmt.where(ScimGroup.active == scim_filter.value)
        elif scim_filter.attribute == "externalId":
            stmt = stmt.where(ScimGroup.external_id == str(scim_filter.value))
            stmt = stmt.where(ScimGroup.active == True)  # noqa: E712
        elif scim_filter.attribute == "userName":
            # Groups don't have userName — return empty.
            list_resp = ScimListResponse(
                total_results=0, start_index=startIndex, items_per_page=0, resources=[]
            )
            return JSONResponse(
                content=list_resp.model_dump(by_alias=True),
                media_type=_SCIM_CONTENT_TYPE,
            )
    else:
        stmt = stmt.where(ScimGroup.active == True)  # noqa: E712

    from sqlalchemy import func as sqlfunc

    count_stmt = select(sqlfunc.count()).select_from(stmt.subquery())
    total = await db.scalar(count_stmt) or 0

    offset = max(0, startIndex - 1)
    rows = (await db.execute(stmt.offset(offset).limit(count))).scalars().all()

    resources = [_group_to_scim(g, request) for g in rows]
    list_resp = ScimListResponse(
        total_results=total,
        start_index=startIndex,
        items_per_page=len(resources),
        resources=resources,
    )
    return JSONResponse(
        content=list_resp.model_dump(by_alias=True),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Groups: GET single
# ---------------------------------------------------------------------------


@router.get(
    "/Groups/{group_id}",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
    name="scim_get_group",
)
async def scim_get_group(
    group_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Return a single Group. 404 if missing or soft-deleted."""
    from sqlalchemy.orm import selectinload

    group = await db.scalar(
        select(ScimGroup)
        .options(selectinload(ScimGroup.members))
        .where(ScimGroup.id == group_id)
    )
    if group is None or not group.active:
        return _scim_error(404, "Group not found")

    return JSONResponse(
        content=_group_to_scim(group, request),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Groups: PATCH
# ---------------------------------------------------------------------------


@router.patch(
    "/Groups/{group_id}",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
)
async def scim_patch_group(
    group_id: str,
    body: ScimPatchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Per-attribute update per RFC 7644 §3.5.2.

    ``op: add`` on ``members`` MUST append, never replace. Azure AD
    sends incremental member additions as PATCH Add; replacing the
    member list would break its sync.
    """
    from sqlalchemy.orm import selectinload

    group = await db.scalar(
        select(ScimGroup)
        .options(selectinload(ScimGroup.members))
        .where(ScimGroup.id == group_id)
    )
    if group is None or not group.active:
        return _scim_error(404, "Group not found")

    member_ops = _apply_group_patch(group, body, db)

    # Build a lookup of existing member user_ids for this group.
    existing_user_ids = {m.user_id for m in group.members}

    for op_dict in member_ops:
        action = op_dict["action"]
        uid = op_dict["user_id"]
        if action == "add" and uid not in existing_user_ids:
            db.add(
                ScimGroupMember(
                    group_id=group.id,
                    user_id=uid,
                    display=op_dict.get("display"),
                )
            )
            existing_user_ids.add(uid)
        elif action == "remove":
            for m in group.members:
                if m.user_id == uid:
                    await db.delete(m)
                    break

    await db.commit()

    # Expire the session's identity-map cache for this group so the
    # re-fetch below sees the just-committed member rows.
    await db.refresh(group)

    # Re-fetch with members loaded after mutation.
    group = await db.scalar(
        select(ScimGroup)
        .options(selectinload(ScimGroup.members))
        .where(ScimGroup.id == group_id)
        .execution_options(populate_existing=True)
    )
    return JSONResponse(
        content=_group_to_scim(group, request),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Groups: PUT
# ---------------------------------------------------------------------------


@router.put(
    "/Groups/{group_id}",
    dependencies=[Depends(require_scim_bearer)],
    response_class=JSONResponse,
)
async def scim_replace_group(
    group_id: str,
    body: ScimGroupSchema,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> JSONResponse:
    """Whole-resource replace. Replaces members list atomically."""
    from sqlalchemy.orm import selectinload

    group = await db.scalar(
        select(ScimGroup)
        .options(selectinload(ScimGroup.members))
        .where(ScimGroup.id == group_id)
    )
    if group is None or not group.active:
        return _scim_error(404, "Group not found")

    group.display_name = body.display_name
    group.external_id = body.external_id

    # Replace members entirely — this is a PUT, not PATCH.
    for m in list(group.members):
        await db.delete(m)
    await db.flush()

    for member in body.members:
        db.add(
            ScimGroupMember(
                group_id=group.id,
                user_id=member.value,
                display=member.display,
            )
        )

    await db.commit()

    # Force a fresh load bypassing the identity-map cache.
    await db.refresh(group)
    group = await db.scalar(
        select(ScimGroup)
        .options(selectinload(ScimGroup.members))
        .where(ScimGroup.id == group_id)
        .execution_options(populate_existing=True)
    )
    return JSONResponse(
        content=_group_to_scim(group, request),
        media_type=_SCIM_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Groups: DELETE (soft)
# ---------------------------------------------------------------------------


@router.delete(
    "/Groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_scim_bearer)],
)
async def scim_delete_group(
    group_id: str,
    db: AsyncSession = Depends(get_db),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> None:
    """Soft-delete: sets ``active=False``. Preserves row + members for audit."""
    group = await db.get(ScimGroup, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ScimError(status="404", detail="Group not found").model_dump(
                by_alias=True, exclude_none=True
            ),
        )
    if not group.active:
        return

    group.active = False
    await db.commit()
