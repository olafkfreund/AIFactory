"""Tenant-isolation namespace routing — from services/agent_service.py (#35/#36).

TenantTarget + resolve_tenant_target, lifted out of the agent_service god-file.
agent_service.py re-exports both so existing callers are unchanged. Imports
nothing from agent_service -> no circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TenantTarget:
    """Where an agent task for one org should land.

    ``namespace`` and ``service_account`` are None when the org runs
    in legacy shared-namespace mode — the caller falls back to the
    deployment-default namespace + SA. ``isolation_mode`` mirrors
    ``tenant_states.isolation_mode``: ``shared`` | ``isolated`` |
    ``deleted``.

    The ``deleted`` mode is surfaced so the spawner can refuse to
    create new agent pods for soft-deleted orgs (design §7 stage-1).
    """

    isolation_mode: str
    namespace: str | None
    service_account: str | None


async def resolve_tenant_target(
    db: Any,
    org_id: str | None,
) -> TenantTarget:
    """Look up the tenant routing target for an agent task.

    The agent spawner calls this before pod-spawn:
      - When the row is missing OR ``isolation_mode='shared'``, the
        caller targets the deployment-default namespace + SA
        (backward compat with pre-#36 deployments).
      - When ``isolation_mode='isolated'``, the caller spawns into
        the per-tenant namespace as the per-tenant SA.
      - When ``isolation_mode='deleted'``, the caller MUST refuse
        to spawn new tasks (existing pods may finish but no new
        creates).

    ``org_id`` may be None for legacy single-tenant deployments
    where projects don't carry an org_id yet; we return shared mode
    so the spawner falls back gracefully.

    Failure-safe: ANY exception (DB error, missing model, etc.)
    falls back to shared mode + logs a warning. The agent spawner
    must never crash because the tenant_state row couldn't be read.
    """
    # WHY: deferred import. The web-server's agent_service is imported
    # by paths that don't always have the database set up (CLI tools,
    # tests); the lazy import keeps that path clean.
    from ..database.models import TenantState

    if not org_id:
        return TenantTarget(
            isolation_mode="shared",
            namespace=None,
            service_account=None,
        )
    try:
        state = await db.get(TenantState, org_id)
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "resolve_tenant_target: DB lookup failed for org=%s; "
            "falling back to shared mode",
            org_id,
            exc_info=True,
        )
        return TenantTarget(
            isolation_mode="shared",
            namespace=None,
            service_account=None,
        )

    if state is None or state.isolation_mode == "shared":
        return TenantTarget(
            isolation_mode="shared",
            namespace=None,
            service_account=None,
        )
    return TenantTarget(
        isolation_mode=state.isolation_mode,
        namespace=state.namespace_name,
        service_account=state.service_account,
    )
