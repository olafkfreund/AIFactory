"""
Audit logging service for security-relevant actions.

Provides functions to create immutable audit log entries in the database.
All logging functions are designed to be non-blocking and failure-safe --
a failed audit log write will never crash the calling operation.

Usage::

    from ..services.audit_service import log_audit_event, ACTION_USER_LOGIN

    # Within a route handler that already has a db session:
    await log_audit_event(
        db=db,
        user_id=user.id,
        org_id=org.id,
        action=ACTION_USER_LOGIN,
        resource_type="user",
        resource_id=user.id,
        ip=request.client.host,
    )

    # From background code without a request-scoped session:
    await log_audit_event_bg(
        user_id=user.id,
        org_id=org.id,
        action=ACTION_USER_LOGIN,
        resource_type="user",
        resource_id=user.id,
    )
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from factory_common.logsafe import sanitize_log
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import AuditLog, engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Action constants
# ---------------------------------------------------------------------------

ACTION_USER_REGISTER = "user.register"
ACTION_USER_LOGIN = "user.login"

ACTION_ORG_CREATE = "org.create"
ACTION_ORG_UPDATE = "org.update"
ACTION_ORG_DELETE = "org.delete"

ACTION_MEMBER_INVITE = "member.invite"
ACTION_MEMBER_REMOVE = "member.remove"
ACTION_MEMBER_ROLE_CHANGE = "member.role_change"

ACTION_PROJECT_CREATE = "project.create"
ACTION_PROJECT_DELETE = "project.delete"

ACTION_TASK_CREATE = "task.create"
ACTION_TASK_START = "task.start"
ACTION_TASK_MERGE = "task.merge"

ACTION_API_KEY_CREATE = "api_key.create"
ACTION_API_KEY_REVOKE = "api_key.revoke"

# Security-relevant negative events (Factory#313 — audit COMPLETENESS gap).
# The hash chain was strong but auth/authz rejections were never recorded, so a
# credential-stuffing or privilege-probing attempt left no chained trail. These
# are emitted at the central chokepoints: the 401 in ``auth.py`` (invalid /
# expired token) and the 403 in ``routes/project_authz.py`` (project-authz
# denial / insufficient-role gate).
ACTION_AUTH_FAILURE = "auth.failure"
ACTION_AUTHZ_DENIED = "authz.denied"
ACTION_GATE_REJECTED = "gate.rejected"

# MCP control-plane actions (Epic #50 acceptance criterion #2).
# Every write tool exposed via the ``/api/mcp-stdio/*`` proxy logs
# its action under the ``mcp.*`` namespace. The mcp prefix keeps these
# distinguishable from equivalent UI-driven actions (e.g. ``task.start``
# from a JWT user vs ``mcp.task.start`` from an ``acw_`` key), which
# matters for compliance review.
ACTION_MCP_PROJECT_CREATE = "mcp.project.create"
ACTION_MCP_TASK_CREATE_AND_RUN = "mcp.task.create_and_run"
ACTION_MCP_TASK_START = "mcp.task.start"
ACTION_MCP_TASK_STOP = "mcp.task.stop"
ACTION_MCP_TASK_RECOVER = "mcp.task.recover"
ACTION_MCP_TASK_APPROVE_PLAN = "mcp.task.approve_plan"
ACTION_MCP_TASK_CREATE_PR = "mcp.task.create_pr"
ACTION_MCP_TASK_MERGE = "mcp.task.merge"


# ---------------------------------------------------------------------------
# Hash-chain helpers (shared by the request-scoped and background paths)
# ---------------------------------------------------------------------------


async def _next_prev_hash(session: AsyncSession) -> str:
    """Return the ``prev_hash`` a new row should carry: the chained hash of the
    current chain head, or ``GENESIS`` for an empty log.

    Epic #26 P5.2 concurrency note: parallel writers across sessions can race so
    two rows share a prev_hash. v1.0 mitigates via the single-replica
    constraint; a SELECT FOR UPDATE on the head lands with multi-replica.
    """
    from sqlalchemy import select as _select

    from .audit_chain import GENESIS, compute_hash, row_as_mapping

    last = await session.execute(
        _select(AuditLog).order_by(AuditLog.created_at.desc()).limit(1)
    )
    last_row = last.scalar_one_or_none()
    if last_row is None:
        return GENESIS
    return compute_hash(last_row.prev_hash, row_as_mapping(last_row))


def _default_retention_until() -> datetime:
    """Default retention: 13 months (SOC2 12mo + buffer)."""
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(days=395)


# ---------------------------------------------------------------------------
# Core audit logging function
# ---------------------------------------------------------------------------


async def log_audit_event(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict | None = None,
    ip: str | None = None,
) -> None:
    """Create an audit log entry using the provided database session.

    This function is wrapped in a try/except so that audit logging
    failures never propagate to the calling code.  A warning is logged
    instead.

    The insert runs inside its own SAVEPOINT so that promise actually
    holds. Without one, a failed flush (a violated FK on ``org_id``/
    ``user_id``, an over-length column) leaves the caller's
    ``AsyncSession`` in a needs-rollback state; the route's next
    ``await db.commit()`` then raises ``PendingRollbackError`` and the
    BUSINESS request 500s -- the audit failure propagated after all,
    just later and wearing the caller's name. Rolling the savepoint back
    restores the session to exactly the state the caller had before, so
    their transaction stays committable.

    A savepoint rather than a separate session (the
    :func:`log_audit_event_bg` route) on purpose: callers pass their own
    session precisely so the audit row lands in the SAME transaction as
    the action it records. A separate session would commit an audit row
    for a business change that then rolled back.

    Parameters
    ----------
    db:
        An active ``AsyncSession`` (typically the request-scoped session).
    user_id:
        The ID of the user who performed the action, or ``None`` for
        system-initiated events.
    org_id:
        The ID of the organization the action belongs to, or ``None``
        for org-independent events (e.g., user registration).
    action:
        A dot-separated action identifier (e.g., ``"user.login"``).
        Use the ``ACTION_*`` constants defined in this module.
    resource_type:
        The type of resource affected (e.g., ``"user"``, ``"org"``,
        ``"project"``).
    resource_id:
        The ID of the specific resource affected, if applicable.
    details:
        Optional dictionary of extra context to store as JSON.
    ip:
        The IP address of the client, if available.
    """
    try:
        # Own savepoint: a failed audit insert must not leave the caller's
        # session in a needs-rollback state (see the docstring). Everything
        # that touches the DB goes inside -- the chain-head SELECT too, since
        # a failed statement poisons the transaction just as an insert does.
        async with db.begin_nested():
            # Epic #26 P5.2 — hash chain on write. This row's prev_hash =
            # compute_hash(current chain head). See ``_next_prev_hash``.
            prev_hash_value = await _next_prev_hash(db)

            entry = AuditLog(
                user_id=user_id,
                org_id=org_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details_json=json.dumps(details) if details is not None else None,
                ip=ip,
                retention_until=_default_retention_until(),
                prev_hash=prev_hash_value,
            )
            db.add(entry)
            await db.flush()
    except Exception:
        logger.warning(
            "Failed to write audit log entry: action=%s resource_type=%s resource_id=%s",
            sanitize_log(action),
            sanitize_log(resource_type),
            sanitize_log(resource_id),
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Background audit logging (creates its own session)
# ---------------------------------------------------------------------------


async def log_audit_event_bg(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict | None = None,
    ip: str | None = None,
) -> None:
    """Create an audit log entry using a self-managed database session.

    This is useful when you need to log an audit event outside of a
    request-scoped session (e.g., from a background task, a WebSocket
    handler, or any code that does not have access to the FastAPI
    ``Depends(get_db)`` dependency).

    The session is created, committed, and closed within this function.
    Like :func:`log_audit_event`, failures are caught and logged as
    warnings so they never crash the caller.

    The row is hash-chained exactly like :func:`log_audit_event`
    (Factory#313 — previously this background path wrote UNCHAINED rows
    with a NULL ``prev_hash``, so security-relevant events emitted from
    background/WebSocket/MCP code and the auth middleware fell outside the
    tamper-evident chain). ``prev_hash`` is now computed from the chain
    head inside the self-managed session.

    Parameters are identical to :func:`log_audit_event` except there is
    no ``db`` parameter.
    """
    try:
        async with engine.async_session_factory() as session:
            prev_hash_value = await _next_prev_hash(session)
            entry = AuditLog(
                user_id=user_id,
                org_id=org_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                details_json=json.dumps(details) if details is not None else None,
                ip=ip,
                retention_until=_default_retention_until(),
                prev_hash=prev_hash_value,
            )
            session.add(entry)
            await session.commit()
    except Exception:
        logger.warning(
            "Failed to write background audit log entry: action=%s resource_type=%s resource_id=%s",
            sanitize_log(action),
            sanitize_log(resource_type),
            sanitize_log(resource_id),
            exc_info=True,
        )
