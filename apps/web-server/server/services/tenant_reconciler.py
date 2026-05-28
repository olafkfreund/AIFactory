"""Tenant Isolation Mode reconciler — dry-run skeleton (Epic #35 #36 PR-1).

What this PR ships
------------------

The decision-logic layer of the reconciler. The reconciler computes
WHAT it would do (create namespace X with SA Y, attach IAM role Z,
provision Vault policy P, etc.) but does NOT call the Kubernetes /
IAM / Vault APIs — those land in PR-2.

This lets us test the decision logic in isolation against in-process
SQLite without spinning up a Kubernetes fixture.

The PR-2 layer will swap the `_log_intent` calls for real API
clients while keeping this module's interface unchanged. Tests
written against this PR continue to pass.

Design references
-----------------

- `docs/plans/2026-05-28-tenant-isolation-design.md` — locked design
  with 8 brainstorm decisions + 6 reviewer-audited refinements.

Failure-safe contract (per design §1)
-------------------------------------

Every reconcile step wraps in try/except. A stuck or failing pass
logs WARNING + writes the failure to `tenant_states.reconcile_error`
+ retries on the next tick. A broken reconciler does NOT crash the
calling lifespan.

Leader-election + actual K8s calls land in PR-2; this PR keeps the
shape so the swap is mechanical.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import Organization, TenantState

logger = logging.getLogger(__name__)


# Operator-tunable in PR-3's Helm chart; for PR-1 these are module
# constants so the decision logic is testable.
_NAMESPACE_PREFIX = os.environ.get(
    "TENANT_NAMESPACE_PREFIX", "aifactory-tenant",
)
_DELETION_GRACE_DAYS = int(
    os.environ.get("TENANT_DELETION_GRACE_DAYS", "30"),
)


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileDecision:
    """The action the reconciler determined this org needs.

    PR-1 emits these; PR-2 acts on them.
    """

    org_id: str
    action: str  # 'create' | 'update' | 'soft_delete_acked' | 'tear_down' | 'no_op'
    target_namespace: str | None
    isolation_mode: str  # 'shared' | 'isolated' | 'deleted'
    rationale: str       # human-readable, ends up in the log line


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_namespace_name(org_slug: str) -> str:
    """Compute the immutable namespace name for an org.

    Called ONCE on first reconcile pass when isolation is enabled.
    The result is stored in ``organizations.tenant_namespace`` and
    NEVER changes on subsequent slug renames (per design decision #2).
    """
    return f"{_NAMESPACE_PREFIX}-{org_slug}"


async def reconcile_org(
    db: AsyncSession, org: Organization, *,
    isolation_enabled: bool,
    now: datetime | None = None,
) -> ReconcileDecision:
    """Compute the reconcile action for ONE Organization. Dry-run.

    The decision is returned + logged but NOT acted on in PR-1.
    Updates ``tenant_states.reconciled_at`` to record the pass.

    ``isolation_enabled`` is the deployment-wide toggle from Helm
    values (``tenant.isolationEnabled``).
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    # Load or create the tenant_state row.
    state = await _load_or_create_state(db, org.id)

    try:
        decision = _compute_decision(
            org=org, state=state,
            isolation_enabled=isolation_enabled, now=now,
        )
        _log_intent(decision)

        # Update the state's reconciled_at + clear any prior error.
        state.reconciled_at = now
        state.reconcile_error = None
        return decision
    except Exception as exc:
        # Failure-safe: record the error, return a no-op decision, do
        # NOT crash. Operator sees the error via SQL query.
        logger.warning(
            "tenant reconciler failed for org %s; will retry next tick",
            org.id, exc_info=True,
        )
        state.reconcile_error = f"{type(exc).__name__}: {exc}"[:512]
        return ReconcileDecision(
            org_id=org.id, action="no_op",
            target_namespace=None,
            isolation_mode=state.isolation_mode,
            rationale=f"reconcile failed: {type(exc).__name__}",
        )


async def reconcile_all(
    db: AsyncSession, *, isolation_enabled: bool,
) -> list[ReconcileDecision]:
    """Periodic sweep: reconcile every Organization. Dry-run."""
    stmt = select(Organization).order_by(Organization.created_at.asc())
    result = await db.execute(stmt)
    decisions: list[ReconcileDecision] = []
    for org in result.scalars():
        decisions.append(
            await reconcile_org(
                db, org, isolation_enabled=isolation_enabled,
            ),
        )
    return decisions


# ---------------------------------------------------------------------------
# Decision logic (pure; testable without DB)
# ---------------------------------------------------------------------------


def _compute_decision(
    *, org: Organization, state: TenantState,
    isolation_enabled: bool, now: datetime,
) -> ReconcileDecision:
    """The heart of the reconciler. Pure function: takes the org +
    current state + toggle, returns what action to take."""

    # Case 1: org is soft-deleted. Two sub-cases:
    if org.deleted_at is not None:
        days_since_delete = (now - org.deleted_at).days
        if days_since_delete >= _DELETION_GRACE_DAYS:
            return ReconcileDecision(
                org_id=org.id, action="tear_down",
                target_namespace=state.namespace_name,
                isolation_mode="deleted",
                rationale=(
                    f"org soft-deleted {days_since_delete} days ago "
                    f">= grace period {_DELETION_GRACE_DAYS}; "
                    f"PR-2 will tear down resources"
                ),
            )
        # Within grace window. Mark soft-delete acknowledged so the
        # agent spawner refuses new tasks; resources stay until day-30.
        return ReconcileDecision(
            org_id=org.id, action="soft_delete_acked",
            target_namespace=state.namespace_name,
            isolation_mode="deleted",
            rationale=(
                f"org soft-deleted {days_since_delete} days ago; "
                f"{_DELETION_GRACE_DAYS - days_since_delete} days "
                f"until tear-down"
            ),
        )

    # Case 2: isolation is disabled. Org stays in 'shared' mode.
    if not isolation_enabled:
        if state.isolation_mode != "shared":
            # Operator flipped the toggle off after enabling it. We
            # don't auto-tear-down; document operator workflow.
            return ReconcileDecision(
                org_id=org.id, action="no_op",
                target_namespace=state.namespace_name,
                isolation_mode=state.isolation_mode,
                rationale=(
                    "isolation toggle is off but org has existing "
                    "isolated state; leave as-is (operator must "
                    "explicitly migrate or hard-delete)"
                ),
            )
        return ReconcileDecision(
            org_id=org.id, action="no_op",
            target_namespace=None,
            isolation_mode="shared",
            rationale="isolation disabled; org runs in shared namespace",
        )

    # Case 3: isolation IS enabled.

    # Case 3a: first reconcile (no namespace assigned yet).
    if state.namespace_name is None:
        target_ns = derive_namespace_name(org.slug)
        return ReconcileDecision(
            org_id=org.id, action="create",
            target_namespace=target_ns,
            isolation_mode="isolated",
            rationale=(
                f"first reconcile under isolation; PR-2 will create "
                f"namespace {target_ns} + ServiceAccount + NetPol + "
                f"S3 prefix + Vault policy"
            ),
        )

    # Case 3b: namespace exists; check for drift.
    if state.isolation_mode != "isolated":
        # The namespace was created at some point but the mode is
        # wrong. This shouldn't happen via the normal path; mark for
        # update.
        return ReconcileDecision(
            org_id=org.id, action="update",
            target_namespace=state.namespace_name,
            isolation_mode="isolated",
            rationale=(
                f"state drift: namespace exists ({state.namespace_name}) "
                f"but isolation_mode={state.isolation_mode}; PR-2 will "
                f"sync mode to 'isolated'"
            ),
        )

    # Case 3c: steady state. Nothing to do.
    return ReconcileDecision(
        org_id=org.id, action="no_op",
        target_namespace=state.namespace_name,
        isolation_mode="isolated",
        rationale="steady state; reconciliation is current",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_or_create_state(
    db: AsyncSession, org_id: str,
) -> TenantState:
    """Get the org's tenant_state row, creating one with default
    'shared' mode if absent. New orgs land in 'shared' until the
    first reconcile pass under enabled isolation upgrades them."""
    stmt = select(TenantState).where(TenantState.org_id == org_id)
    result = await db.execute(stmt)
    state = result.scalar_one_or_none()
    if state is not None:
        return state

    state = TenantState(org_id=org_id, isolation_mode="shared")
    db.add(state)
    await db.flush()
    return state


def _log_intent(decision: ReconcileDecision) -> None:
    """PR-1: just log the decision at INFO. PR-2 will swap this for
    real K8s/IAM/Vault writes."""
    logger.info(
        "tenant reconciler DRY-RUN: org=%s action=%s ns=%s mode=%s — %s",
        decision.org_id, decision.action,
        decision.target_namespace, decision.isolation_mode,
        decision.rationale,
    )
