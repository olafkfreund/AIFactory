"""Tenant Isolation Mode reconciler (Epic #35 #36).

PR-1 shipped the decision-logic layer (dry-run). PR-2 wires in real
Kubernetes / IAM / Vault writes behind the same decision interface
+ adds Redis-based leader election so multi-replica deployments
don't race.

Design references
-----------------

- `docs/plans/2026-05-28-tenant-isolation-design.md` — locked design
  with 8 brainstorm decisions + 6 reviewer-audited refinements.
- §1 reconciler home + leader election (Redis SETNX mutex per org-id).
- §3 NetworkPolicy egress + CNI capability lock.
- §4 per-tenant IAM role + hard-coded prefix.
- §4a S3 recursive-delete safety (prefix-shape regex).
- §5 Vault per-tenant path + reconciler AppRole.
- §6 agent spawner reads ``tenant_states``.
- §7 tear-down stage-2 + stuck-Terminating detection.

Failure-safe contract (per design §1)
-------------------------------------

Every reconcile step wraps in try/except. A stuck or failing pass
logs WARNING + writes the failure to ``tenant_states.reconcile_error``
+ retries on the next tick. A broken reconciler does NOT crash the
calling lifespan. Cloud-side failures (AWS not configured / Vault
unreachable) trigger SKIP, not abort, so the K8s side can still
land.

Leader election
---------------

Before any K8s/IAM/Vault write the reconciler acquires a per-org
Redis lock via ``SETNX aifactory:tenant-recon-lock:<org_id> <pod_id>
EX 60``. Released via a Lua check-and-delete so we never drop
another replica's lock. If Redis is unavailable the reconciler
REFUSES to write and logs ERROR once — single-replica is the only
safe mode without leader election.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import Organization, TenantState

logger = logging.getLogger(__name__)


# Operator-tunable in PR-3's Helm chart; for now these are module
# constants so the decision logic is testable.
_NAMESPACE_PREFIX = os.environ.get(
    "TENANT_NAMESPACE_PREFIX",
    "aifactory-tenant",
)
_DELETION_GRACE_DAYS = int(
    os.environ.get("TENANT_DELETION_GRACE_DAYS", "30"),
)

# Cloud backend selector — gates AWS-only steps. ``aws`` exercises
# the IAM/IRSA path; anything else (e.g. ``gcp``, ``azure``, ``none``)
# skips IAM + assumes operator-supplied per-tenant credentials.
_CLOUD_BACKEND = os.environ.get("TENANT_CLOUD_BACKEND", "aws").lower()

# Per-pod identity. Stable for the process lifetime; used as the
# lock value so we never release another replica's lock.
_POD_ID: str = str(uuid.uuid4())

# Leader-election lock prefix.
_LOCK_PREFIX = "aifactory:tenant-recon-lock"
_LOCK_TTL_SEC = 60

# Redis Lua script: delete the lock IFF the value matches ours.
# Avoids the classic "release someone else's lock after expiry" race.
_LUA_UNLOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) "
    "else return 0 end"
)

# Stuck-Terminating threshold (design §7 stage-2).
_STUCK_TERMINATING_MIN = 30

# CNI backend selector (calico|cilium|auto). Operator override via
# env; PR-3 wires this into Helm values.
_CNI_BACKEND = os.environ.get("TENANT_CNI_BACKEND", "auto").lower()

# S3 bucket for tenant prefixes (operator-supplied). Empty = skip
# S3 tear-down.
_TENANT_S3_BUCKET = os.environ.get("TENANT_S3_BUCKET", "")

# IRSA OIDC provider ARN. Empty = skip IAM role creation.
_TENANT_OIDC_PROVIDER_ARN = os.environ.get("TENANT_OIDC_PROVIDER_ARN", "")

# Allowed-FQDN egress list. Comma-separated; PR-3's Helm chart
# materialises this from ``tenant.networkPolicy.allowedFqdns``.
_ALLOWED_FQDNS_RAW = os.environ.get(
    "TENANT_ALLOWED_FQDNS",
    "api.anthropic.com",
)

# ResourceQuota / LimitRange defaults — operator-tunable in PR-3.
_QUOTA_POD_COUNT = int(os.environ.get("TENANT_QUOTA_PODS", "50"))
_QUOTA_PVC_COUNT = int(os.environ.get("TENANT_QUOTA_PVCS", "10"))
_LIMIT_DEFAULT_CPU = os.environ.get("TENANT_LIMIT_CPU", "500m")
_LIMIT_DEFAULT_MEMORY = os.environ.get("TENANT_LIMIT_MEMORY", "512Mi")


# ---------------------------------------------------------------------------
# Decision types (unchanged from PR-1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileDecision:
    """The action the reconciler determined this org needs."""

    org_id: str
    action: str  # 'create' | 'update' | 'soft_delete_acked' | 'tear_down' | 'no_op'
    target_namespace: str | None
    isolation_mode: str  # 'shared' | 'isolated' | 'deleted'
    rationale: str  # human-readable, ends up in the log line


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_namespace_name(org_slug: str) -> str:
    """Compute the immutable namespace name for an org (design §2)."""
    return f"{_NAMESPACE_PREFIX}-{org_slug}"


def derive_service_account_name(org_slug: str) -> str:
    """Compute the per-tenant ServiceAccount name (one per namespace)."""
    return f"{_NAMESPACE_PREFIX}-{org_slug}-agent"


def _parse_allowed_fqdns() -> list[str]:
    return [f.strip() for f in _ALLOWED_FQDNS_RAW.split(",") if f.strip()]


async def reconcile_org(
    db: AsyncSession,
    org: Organization,
    *,
    isolation_enabled: bool,
    now: datetime | None = None,
    apply: bool = False,
    k8s_client: Any = None,
    aws_client: Any = None,
    vault_client: Any = None,
    redis_client: Any = None,
) -> ReconcileDecision:
    """Compute the reconcile action for ONE Organization.

    Default mode (``apply=False``) returns the decision without doing
    any cloud-side writes — preserves the PR-1 contract so the existing
    test suite + caller dependencies don't break.

    When ``apply=True`` AND the decision is ``create``/``update``/
    ``tear_down`` AND the per-org Redis lock is acquired, the
    reconciler invokes K8s + AWS + Vault writes in order. The
    individual writers are failure-safe: a Vault outage doesn't roll
    back the K8s namespace (the next pass retries Vault only).

    Failures during apply land in ``tenant_states.reconcile_error``;
    the decision is still returned so callers can audit-log the
    intent regardless of write outcome.
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)

    state = await _load_or_create_state(db, org.id)

    try:
        decision = _compute_decision(
            org=org,
            state=state,
            isolation_enabled=isolation_enabled,
            now=now,
        )
        _log_intent(decision)

        if apply and decision.action in ("create", "update"):
            await _apply_create_or_update(
                db=db,
                org=org,
                state=state,
                decision=decision,
                k8s_client=k8s_client,
                aws_client=aws_client,
                vault_client=vault_client,
                redis_client=redis_client,
            )
        elif apply and decision.action == "tear_down":
            await _apply_tear_down(
                db=db,
                org=org,
                state=state,
                k8s_client=k8s_client,
                aws_client=aws_client,
                vault_client=vault_client,
                redis_client=redis_client,
            )

        # Update the state's reconciled_at + clear any prior error
        # iff we got this far without re-raising.
        state.reconciled_at = now
        if state.reconcile_error and decision.action in (
            "no_op",
            "soft_delete_acked",
        ):
            # Steady-state passes shouldn't keep an old error sticky.
            state.reconcile_error = None
        elif decision.action not in ("create", "update", "tear_down"):
            state.reconcile_error = None
        return decision
    except Exception as exc:
        # Failure-safe: record the error, return a no-op decision, do
        # NOT crash. Operator sees the error via SQL query.
        logger.warning(
            "tenant reconciler failed for org %s; will retry next tick",
            org.id,
            exc_info=True,
        )
        state.reconcile_error = f"{type(exc).__name__}: {exc}"[:512]
        return ReconcileDecision(
            org_id=org.id,
            action="no_op",
            target_namespace=None,
            isolation_mode=state.isolation_mode,
            rationale=f"reconcile failed: {type(exc).__name__}",
        )


async def reconcile_all(
    db: AsyncSession,
    *,
    isolation_enabled: bool,
    apply: bool = False,
    k8s_client: Any = None,
    aws_client: Any = None,
    vault_client: Any = None,
    redis_client: Any = None,
) -> list[ReconcileDecision]:
    """Periodic sweep: reconcile every Organization."""
    stmt = select(Organization).order_by(Organization.created_at.asc())
    result = await db.execute(stmt)
    decisions: list[ReconcileDecision] = []
    for org in result.scalars():
        decisions.append(
            await reconcile_org(
                db,
                org,
                isolation_enabled=isolation_enabled,
                apply=apply,
                k8s_client=k8s_client,
                aws_client=aws_client,
                vault_client=vault_client,
                redis_client=redis_client,
            ),
        )
    return decisions


async def tear_down_org(
    db: AsyncSession,
    org_id: str,
    *,
    dry_run: bool = True,
    k8s_client: Any = None,
    aws_client: Any = None,
    vault_client: Any = None,
    redis_client: Any = None,
) -> dict[str, Any]:
    """Stage-2 tear-down entry point (per design §7 stage-2).

    The daily cron in PR-3 calls this; for PR-2 it's exposed so the
    test suite can verify the safety regex + ordering. ``dry_run=True``
    is the 24-hour pre-delete pass (S3 prefix-shape assertion is
    still enforced; objects are LISTED not deleted).

    Returns a dict describing what was done (or would be done) — the
    caller logs this to the audit chain.
    """
    org = await db.get(Organization, org_id)
    if org is None:
        return {"status": "skipped", "reason": "org row gone"}
    state = await db.get(TenantState, org_id)
    if state is None or state.namespace_name is None:
        return {"status": "skipped", "reason": "no tenant_state to tear down"}

    # Leader-election: required for the destructive path even more so
    # than the create path.
    lock_acquired = await _acquire_lock(redis_client, org_id)
    if not lock_acquired:
        return {"status": "skipped", "reason": "lock not acquired"}

    try:
        result: dict[str, Any] = {
            "org_id": org_id,
            "dry_run": dry_run,
            "namespace": state.namespace_name,
            "s3_objects_count": 0,
            "vault_deleted": False,
            "k8s_deleted": False,
            "iam_deleted": False,
        }

        # 1. S3 first — irreversible. Dry-run lets the operator inspect.
        if aws_client is not None and _TENANT_S3_BUCKET:
            prefix = f"orgs/{org.id}/"
            try:
                # ValueError from §4a regex check propagates so callers
                # see the safety violation; everything else stays inside
                # the try/except per failure-safe contract.
                count = await aws_client.s3_recursive_delete(
                    bucket=_TENANT_S3_BUCKET,
                    prefix=prefix,
                    dry_run=dry_run,
                )
                result["s3_objects_count"] = count
            except ValueError:
                raise
            except Exception:
                logger.warning(
                    "tear_down S3 step failed for org %s",
                    org_id,
                    exc_info=True,
                )

        # 2. K8s namespace delete (cascades to child resources).
        if not dry_run and k8s_client is not None:
            try:
                await k8s_client.delete_namespace(state.namespace_name)
                result["k8s_deleted"] = True
            except Exception:
                logger.warning(
                    "tear_down K8s delete failed for org %s",
                    org_id,
                    exc_info=True,
                )

        # 3. Vault policy + role.
        if not dry_run and vault_client is not None:
            try:
                # The Vault client is sync; call directly.
                vault_client.delete_tenant_policy_and_role(org.id)
                result["vault_deleted"] = True
            except Exception:
                logger.warning(
                    "tear_down Vault delete failed for org %s",
                    org_id,
                    exc_info=True,
                )

        # 4. IAM role.
        if not dry_run and aws_client is not None:
            try:
                await aws_client.delete_tenant_role(org.id)
                result["iam_deleted"] = True
            except Exception:
                logger.warning(
                    "tear_down IAM delete failed for org %s",
                    org_id,
                    exc_info=True,
                )

        # 5. Mark the state row torn-down (PR-3 cron will delete it).
        if not dry_run:
            state.isolation_mode = "deleted"
            state.reconcile_error = None

        return result
    finally:
        await _release_lock(redis_client, org_id)


# ---------------------------------------------------------------------------
# Decision logic (pure; testable without DB or clients) — unchanged from PR-1
# ---------------------------------------------------------------------------


def _compute_decision(
    *,
    org: Organization,
    state: TenantState,
    isolation_enabled: bool,
    now: datetime,
) -> ReconcileDecision:
    """The heart of the reconciler. Pure function."""

    # Case 1: org is soft-deleted. Two sub-cases:
    if org.deleted_at is not None:
        days_since_delete = (now - org.deleted_at).days
        if days_since_delete >= _DELETION_GRACE_DAYS:
            return ReconcileDecision(
                org_id=org.id,
                action="tear_down",
                target_namespace=state.namespace_name,
                isolation_mode="deleted",
                rationale=(
                    f"org soft-deleted {days_since_delete} days ago "
                    f">= grace period {_DELETION_GRACE_DAYS}; "
                    f"PR-2 will tear down resources"
                ),
            )
        return ReconcileDecision(
            org_id=org.id,
            action="soft_delete_acked",
            target_namespace=state.namespace_name,
            isolation_mode="deleted",
            rationale=(
                f"org soft-deleted {days_since_delete} days ago; "
                f"{_DELETION_GRACE_DAYS - days_since_delete} days "
                f"until tear-down"
            ),
        )

    # Case 2: isolation is disabled.
    if not isolation_enabled:
        if state.isolation_mode != "shared":
            return ReconcileDecision(
                org_id=org.id,
                action="no_op",
                target_namespace=state.namespace_name,
                isolation_mode=state.isolation_mode,
                rationale=(
                    "isolation toggle is off but org has existing "
                    "isolated state; leave as-is (operator must "
                    "explicitly migrate or hard-delete)"
                ),
            )
        return ReconcileDecision(
            org_id=org.id,
            action="no_op",
            target_namespace=None,
            isolation_mode="shared",
            rationale="isolation disabled; org runs in shared namespace",
        )

    # Case 3: isolation IS enabled.

    # Case 3a: first reconcile (no namespace assigned yet).
    if state.namespace_name is None:
        target_ns = derive_namespace_name(org.slug)
        return ReconcileDecision(
            org_id=org.id,
            action="create",
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
        return ReconcileDecision(
            org_id=org.id,
            action="update",
            target_namespace=state.namespace_name,
            isolation_mode="isolated",
            rationale=(
                f"state drift: namespace exists ({state.namespace_name}) "
                f"but isolation_mode={state.isolation_mode}; PR-2 will "
                f"sync mode to 'isolated'"
            ),
        )

    # Case 3c: steady state.
    return ReconcileDecision(
        org_id=org.id,
        action="no_op",
        target_namespace=state.namespace_name,
        isolation_mode="isolated",
        rationale="steady state; reconciliation is current",
    )


# ---------------------------------------------------------------------------
# Apply step — orchestrates K8s + AWS + Vault writes
# ---------------------------------------------------------------------------


async def _apply_create_or_update(
    *,
    db: AsyncSession,
    org: Organization,
    state: TenantState,
    decision: ReconcileDecision,
    k8s_client: Any,
    aws_client: Any,
    vault_client: Any,
    redis_client: Any,
) -> None:
    """Execute the K8s + AWS + Vault writes for one org.

    Ordering (design §6):
      1. K8s namespace + SA (without IRSA annotation yet) + Role/Binding
         + NetworkPolicy + Quota + LimitRange.
      2. AWS IAM role (when cloud_backend=aws). PATCH the SA with the
         IRSA annotation once we have the ARN.
      3. Vault policy + K8s-auth role.

    Each step is wrapped so a downstream failure doesn't crash the
    upstream successes. The reconciler retries on the next tick.
    """
    if decision.target_namespace is None:
        raise RuntimeError(
            "decision is create/update but target_namespace is None — "
            "_compute_decision contract violation",
        )

    # Leader election — bail out fast if we don't hold the lock.
    lock_acquired = await _acquire_lock(redis_client, org.id)
    if not lock_acquired:
        logger.debug(
            "lock not acquired for org %s; another replica is reconciling",
            org.id,
        )
        return

    try:
        namespace = decision.target_namespace
        sa_name = derive_service_account_name(org.slug)
        allowed_fqdns = _parse_allowed_fqdns()

        # ----- Step 1: K8s -----
        if k8s_client is None:
            raise RuntimeError(
                "k8s_client required for apply; reconciler caller must inject one",
            )

        labels = {
            "aifactory.io/tenant": org.id,
            "aifactory.io/org-slug": org.slug,
            "aifactory.io/managed-by": "tenant-reconciler",
        }
        await k8s_client.create_tenant_namespace(namespace, labels)
        # SA created without IRSA annotation; we patch it after the
        # IAM role ARN is known.
        await k8s_client.create_service_account(
            namespace=namespace,
            name=sa_name,
            irsa_role_arn=None,
        )
        await k8s_client.create_role_and_binding(
            namespace=namespace,
            sa_name=sa_name,
        )
        await k8s_client.apply_network_policies(
            namespace=namespace,
            allowed_fqdns=allowed_fqdns,
            cni_backend=_CNI_BACKEND,
        )
        await k8s_client.apply_resource_quota(
            namespace=namespace,
            pod_count=_QUOTA_POD_COUNT,
            pvc_count=_QUOTA_PVC_COUNT,
        )
        await k8s_client.apply_limit_range(
            namespace=namespace,
            default_cpu=_LIMIT_DEFAULT_CPU,
            default_memory=_LIMIT_DEFAULT_MEMORY,
        )

        # ----- Step 2: AWS IAM (when applicable) -----
        iam_role_arn: str | None = None
        if (
            _CLOUD_BACKEND == "aws"
            and aws_client is not None
            and _TENANT_OIDC_PROVIDER_ARN
            and _TENANT_S3_BUCKET
        ):
            try:
                iam_role_arn = await aws_client.create_tenant_role(
                    org_uuid=org.id,
                    bucket=_TENANT_S3_BUCKET,
                    oidc_provider_arn=_TENANT_OIDC_PROVIDER_ARN,
                    sa_namespace=namespace,
                    sa_name=sa_name,
                )
                # Re-create / patch SA to land the IRSA annotation.
                await k8s_client.create_service_account(
                    namespace=namespace,
                    name=sa_name,
                    irsa_role_arn=iam_role_arn,
                )
            except Exception:
                # AWS-side failure is non-fatal: K8s namespace is
                # already up; record the error + move on. The next
                # reconcile pass retries.
                logger.warning(
                    "AWS IAM step failed for org %s; K8s namespace "
                    "still created, will retry next tick",
                    org.id,
                    exc_info=True,
                )
                state.reconcile_error = "iam-create failed; see logs"
        else:
            logger.debug(
                "skipping AWS IAM step (cloud_backend=%s, oidc=%s, "
                "bucket=%s, client=%s)",
                _CLOUD_BACKEND,
                bool(_TENANT_OIDC_PROVIDER_ARN),
                bool(_TENANT_S3_BUCKET),
                bool(aws_client),
            )

        # ----- Step 3: Vault -----
        vault_policy: str | None = None
        if vault_client is not None:
            try:
                vault_policy = vault_client.create_tenant_policy(org.id)
                vault_client.create_tenant_kubernetes_role(
                    org_uuid=org.id,
                    sa_namespace=namespace,
                    sa_name=sa_name,
                    policy_name=vault_policy,
                )
            except Exception:
                logger.warning(
                    "Vault step failed for org %s; K8s+IAM landed, "
                    "will retry Vault next tick",
                    org.id,
                    exc_info=True,
                )
                if not state.reconcile_error:
                    state.reconcile_error = "vault-create failed; see logs"
        else:
            logger.debug("skipping Vault step (no client configured)")

        # ----- Step 4: Persist state -----
        state.isolation_mode = "isolated"
        state.namespace_name = namespace
        state.service_account = sa_name
        if iam_role_arn:
            state.iam_role_arn = iam_role_arn
        if vault_policy:
            state.vault_policy_name = vault_policy
        # Lock the immutable namespace name on the org row (design §2).
        if org.tenant_namespace is None:
            org.tenant_namespace = namespace

        # ----- Step 5: Per-tenant audit-anchor key issuance (v1.2 #208) -----
        # Issue a per-tenant HMAC key when the feature flag is on. This runs
        # AFTER K8s/IAM/Vault succeed so the tenant is functional even if key
        # issuance fails (failure is non-fatal per design open-question #2:
        # reconcile_error gets a warning string; the tenant falls back to the
        # shared chain until the next tick retries issuance).
        from .per_tenant_anchor_key import issue_tenant_anchor_key, per_tenant_enabled

        if per_tenant_enabled():
            try:
                issued = await issue_tenant_anchor_key(
                    db,
                    vault_client,
                    org.id,
                )
                if issued:
                    logger.info(
                        "tenant reconciler: per-tenant anchor key issued for org=%s",
                        org.id,
                    )
            except Exception:
                # Non-fatal: the tenant is operational on the shared chain;
                # the next reconcile tick retries key issuance.
                logger.warning(
                    "tenant reconciler: per-tenant anchor key issuance failed "
                    "for org=%s; falling back to shared chain until next tick",
                    org.id,
                    exc_info=True,
                )
                if not state.reconcile_error:
                    state.reconcile_error = "anchor-key-issue failed; see logs"
    finally:
        await _release_lock(redis_client, org.id)


async def _apply_tear_down(
    *,
    db: AsyncSession,
    org: Organization,
    state: TenantState,
    k8s_client: Any,
    aws_client: Any,
    vault_client: Any,
    redis_client: Any,
) -> None:
    """Stage-2 tear-down driven by ``_compute_decision`` (post-grace).

    Delegates to ``tear_down_org`` so the cron path + reconciler path
    share one implementation.
    """
    # tear_down_org handles its own lock acquisition.
    await tear_down_org(
        db,
        org.id,
        dry_run=False,
        k8s_client=k8s_client,
        aws_client=aws_client,
        vault_client=vault_client,
        redis_client=redis_client,
    )


# ---------------------------------------------------------------------------
# Leader election (Redis SETNX with check-and-delete release)
# ---------------------------------------------------------------------------


async def _acquire_lock(redis_client: Any, org_id: str) -> bool:
    """SETNX-based per-org lock.

    Returns True iff this replica acquired the lock. When Redis is
    None (single-replica deployment) we acquire unconditionally — the
    caller has accepted the single-replica trade-off in that case.

    When Redis is configured but unreachable we LOG ERROR + REFUSE
    to write (returns False). Race-free multi-replica needs a working
    Redis; better to skip than to write twice.
    """
    if redis_client is None:
        return True
    key = f"{_LOCK_PREFIX}:{org_id}"
    try:
        # SETNX with TTL atomically. ``set(..., nx=True, ex=...)`` is
        # the modern Redis client form.
        result = await redis_client.set(
            key,
            _POD_ID,
            nx=True,
            ex=_LOCK_TTL_SEC,
        )
        return bool(result)
    except Exception:
        logger.error(
            "tenant reconciler: Redis unavailable; refusing to write "
            "(single-replica mode required when REDIS_URL is set but "
            "Redis is down). org=%s",
            org_id,
            exc_info=True,
        )
        return False


async def _release_lock(redis_client: Any, org_id: str) -> None:
    """Release the per-org lock IFF we still own it (Lua check-and-delete).

    No-op when Redis is None or the lock has already expired.
    """
    if redis_client is None:
        return
    key = f"{_LOCK_PREFIX}:{org_id}"
    try:
        await redis_client.eval(_LUA_UNLOCK, 1, key, _POD_ID)
    except Exception:
        # Best-effort. A stale lock just expires via TTL.
        logger.debug(
            "release lock failed for org %s (will expire via TTL)",
            org_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Stuck-Terminating detection (design §7 stage-2)
# ---------------------------------------------------------------------------


async def _check_stuck_terminating(
    *,
    k8s_client: Any,
    namespace: str,
    now: datetime,
) -> bool:
    """Return True + emit WARNING when the namespace has been Terminating
    for more than _STUCK_TERMINATING_MIN minutes.

    Does NOT force-remove the Namespace finalizer (per design §7
    stage-2: "force-finalizer-removal NOT automated"). The operator
    decides whether to intervene. The WARNING fires every reconcile
    tick until the namespace is gone.
    """
    if k8s_client is None:
        return False
    try:
        status = await k8s_client.get_namespace_status(namespace)
    except Exception:
        logger.debug(
            "stuck-Terminating probe failed for ns %s",
            namespace,
            exc_info=True,
        )
        return False

    if status.phase != "Terminating" or status.deletion_timestamp is None:
        return False

    # The deletion_timestamp is RFC3339 from the SDK; parse it.
    try:
        dt = datetime.fromisoformat(
            status.deletion_timestamp.replace("Z", "+00:00"),
        ).replace(tzinfo=None)
    except ValueError:
        return False

    if (now - dt) > timedelta(minutes=_STUCK_TERMINATING_MIN):
        logger.warning(
            "namespace %s stuck Terminating since %s — operator must "
            "investigate finalizers (auto-removal NOT enabled)",
            namespace,
            status.deletion_timestamp,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_or_create_state(
    db: AsyncSession,
    org_id: str,
) -> TenantState:
    """Get the org's tenant_state row, creating one with default
    'shared' mode if absent."""
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
    """Log the decision at INFO. Distinct prefix from PR-1's "DRY-RUN"
    so operators can grep for live-apply vs dry-run runs."""
    logger.info(
        "tenant reconciler: org=%s action=%s ns=%s mode=%s — %s",
        decision.org_id,
        decision.action,
        decision.target_namespace,
        decision.isolation_mode,
        decision.rationale,
    )


# ---------------------------------------------------------------------------
# Facade — object form for callers that prefer dependency-injection style
# ---------------------------------------------------------------------------


class TenantReconciler:
    """Bundles the K8s/AWS/Vault/Redis clients into one callable.

    Most internal callers use the module-level ``reconcile_org`` /
    ``reconcile_all`` / ``tear_down_org`` directly — the facade is for
    callers (e.g. the FastAPI lifespan in PR-3) who want a single
    object to inject. Plain wrappers; no extra logic.
    """

    def __init__(
        self,
        *,
        k8s_client: Any = None,
        aws_client: Any = None,
        vault_client: Any = None,
        redis_client: Any = None,
        isolation_enabled: bool = False,
    ) -> None:
        self.k8s_client = k8s_client
        self.aws_client = aws_client
        self.vault_client = vault_client
        self.redis_client = redis_client
        self.isolation_enabled = isolation_enabled

    async def reconcile_org(
        self,
        db: AsyncSession,
        org: Organization,
        *,
        apply: bool = True,
    ) -> ReconcileDecision:
        return await reconcile_org(
            db,
            org,
            isolation_enabled=self.isolation_enabled,
            apply=apply,
            k8s_client=self.k8s_client,
            aws_client=self.aws_client,
            vault_client=self.vault_client,
            redis_client=self.redis_client,
        )

    async def reconcile_all(
        self,
        db: AsyncSession,
        *,
        apply: bool = True,
    ) -> list[ReconcileDecision]:
        return await reconcile_all(
            db,
            isolation_enabled=self.isolation_enabled,
            apply=apply,
            k8s_client=self.k8s_client,
            aws_client=self.aws_client,
            vault_client=self.vault_client,
            redis_client=self.redis_client,
        )

    async def tear_down_org(
        self,
        db: AsyncSession,
        org_id: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return await tear_down_org(
            db,
            org_id,
            dry_run=dry_run,
            k8s_client=self.k8s_client,
            aws_client=self.aws_client,
            vault_client=self.vault_client,
            redis_client=self.redis_client,
        )


# ---------------------------------------------------------------------------
# Epic #35 #38 PR-2b — LiteLLM virtual-key lifecycle
# ---------------------------------------------------------------------------
#
# Per the locked design (implementation plan PR-2 + reviewer
# recommendation #5), the reconciler owns per-tenant LiteLLM admin-API
# sync alongside its existing K8s/IAM/Vault decision interface. The
# lifecycle hooks below run on org create / soft-delete / hard-delete
# and a periodic drift sweep reconciles AIFactory's `organizations`
# table against LiteLLM's `/key/list` for divergence after a DB
# restore.
#
# Coordination note: #36 PR-2 is being authored in parallel on
# `feat/36-tenant-resources` and lands K8s/IAM/Vault apply methods on
# this same module. The hooks below intentionally use distinct method
# names so the merge conflict is method-coexistence (textual) not
# behaviour-coexistence (semantic).
#
# Failure-safe contract: every method wraps the admin-client call in
# try/except. A LiteLLM admin failure during reconcile logs WARNING
# + the next reconciler tick retries.


async def sync_virtual_key_on_create(
    org: Organization,
    admin_client,
) -> str | None:
    """Provision a LiteLLM virtual key for a newly-created org.

    Called from the reconciler when the decision is ``action='create'``
    OR from the org-create route post-commit (whichever runs first
    wins; the second is a no-op due to ``user_id`` uniqueness on the
    LiteLLM side, which surfaces as a HTTP 409 we treat as success).

    Returns the generated key string on success, ``None`` on a
    failure that the reconciler should retry next tick.
    """
    try:
        # WHY: pull allowed_models off the ORM row at call-time so
        # the value reflects the most recent UI update. Stale values
        # only surface 1 reconcile tick later.
        key = await admin_client.create_virtual_key(
            org_id=org.id,
            allowed_models=list(org.allowed_models or ["*"]),
        )
        logger.info(
            "litellm virtual-key created: org=%s allowed_models=%r",
            org.id,
            org.allowed_models,
        )
        return key
    except Exception:
        # Failure-safe: log + return None; next tick retries.
        logger.warning(
            "litellm virtual-key create failed for org=%s; "
            "will retry on next reconcile tick",
            org.id,
            exc_info=True,
        )
        return None


async def sync_virtual_key_on_soft_delete(
    org: Organization,
    admin_client,
) -> bool:
    """Disable a LiteLLM virtual key (budget_duration=0) on soft-delete.

    Records an audit row noting the disable (so SOC2 evidence can
    show the enforcement event, not just the org-delete event).
    Returns True on success, False on failure (next tick retries).
    """
    try:
        await admin_client.disable_virtual_key(org_id=org.id)
        logger.info(
            "litellm virtual-key disabled (soft-delete): org=%s",
            org.id,
        )
        # Best-effort audit record. The disable is the
        # enforcement-relevant event; the org.delete row already
        # exists via the orgs route.
        try:
            from .audit_service import log_audit_event_bg

            await log_audit_event_bg(
                org_id=org.id,
                user_id=None,
                action="llm.virtual_key.disabled",
                resource_type="litellm_key",
                resource_id=org.id,
                details={"reason": "org_soft_delete"},
            )
        except Exception:
            # Audit failure is non-fatal; the LiteLLM-side disable
            # already happened, which is the security-critical part.
            logger.debug(
                "litellm virtual-key disable audit write failed for "
                "org=%s; LiteLLM state is correct",
                org.id,
                exc_info=True,
            )
        return True
    except Exception:
        logger.warning(
            "litellm virtual-key disable failed for org=%s; "
            "will retry on next reconcile tick",
            org.id,
            exc_info=True,
        )
        return False


async def sync_virtual_key_on_hard_delete(
    org: Organization,
    admin_client,
) -> bool:
    """Hard-delete a LiteLLM virtual key on tear-down (day 30 per #36).

    Returns True on success; False on failure (next tick retries).
    """
    try:
        await admin_client.delete_virtual_key(org_id=org.id)
        logger.info(
            "litellm virtual-key hard-deleted: org=%s",
            org.id,
        )
        return True
    except Exception:
        logger.warning(
            "litellm virtual-key hard-delete failed for org=%s; "
            "will retry on next reconcile tick",
            org.id,
            exc_info=True,
        )
        return False


async def reconcile_virtual_keys_drift(
    db: AsyncSession,
    admin_client,
) -> dict[str, int]:
    """Periodic drift sweep: align LiteLLM key state with AIFactory orgs.

    Two divergence cases covered:
    1. Org row exists but no LiteLLM key (DB restore from before the
       key was provisioned) → create.
    2. LiteLLM key exists but org row gone (LiteLLM DB restored from
       a later backup than AIFactory's) → delete.

    Returns counts ``{"created": N, "revoked": M, "errors": K}`` for
    operator observability. Never raises — drift recovery is
    best-effort and a failed sweep just retries next tick.
    """
    counts = {"created": 0, "revoked": 0, "errors": 0}
    try:
        live_keys = await admin_client.list_virtual_keys()
    except Exception:
        logger.warning(
            "litellm drift sweep: list_virtual_keys failed; aborting this tick",
            exc_info=True,
        )
        counts["errors"] += 1
        return counts

    # LiteLLM tags keys with the org id via the metadata.aifactory_org_id
    # field (set in create_virtual_key). Build the set of org ids that
    # currently have a key.
    live_org_ids: set[str] = set()
    for key in live_keys:
        metadata = key.get("metadata") or {}
        if isinstance(metadata, dict):
            org_id = metadata.get("aifactory_org_id")
            if isinstance(org_id, str):
                live_org_ids.add(org_id)

    # Active orgs (not soft-deleted) should each have a key.
    stmt = select(Organization).where(Organization.deleted_at.is_(None))
    result = await db.execute(stmt)
    active_orgs = list(result.scalars())
    active_org_ids = {org.id for org in active_orgs}

    # Case 1: orgs missing a key.
    for org in active_orgs:
        if org.id in live_org_ids:
            continue
        key = await sync_virtual_key_on_create(org, admin_client)
        if key is not None:
            counts["created"] += 1
        else:
            counts["errors"] += 1

    # Case 2: keys with no org (or org soft-deleted).
    for org_id in live_org_ids - active_org_ids:
        try:
            await admin_client.delete_virtual_key(org_id=org_id)
            counts["revoked"] += 1
            logger.info(
                "litellm drift sweep: revoked orphan key for org=%s",
                org_id,
            )
        except Exception:
            counts["errors"] += 1
            logger.warning(
                "litellm drift sweep: orphan key revoke failed for "
                "org=%s; will retry next tick",
                org_id,
                exc_info=True,
            )

    return counts
