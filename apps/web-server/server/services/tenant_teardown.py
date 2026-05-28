"""Stage-2 tenant tear-down sweep (Epic #35 #36 PR-3).

What this CLI does
------------------

Loaded by the daily ``teardown-cron.yaml`` Kubernetes CronJob, this
module:

  1. Connects to the same Postgres as the web pod.
  2. SELECTs every ``Organization`` with ``deleted_at IS NOT NULL``
     and ``(now - deleted_at) > TENANT_DELETION_GRACE_DAYS days``.
  3. For each, calls ``tenant_reconciler.tear_down_org(org_id)`` —
     the K8s + S3 + Vault destructor that lands in PR-2.
  4. Exits 0 unconditionally. Per-org failures are caught and
     recorded in ``tenant_states.reconcile_error`` (the reconciler's
     failure-safe contract); they do NOT cause the cron to fail in
     a way that would page on-call out of hours.

What happens BEFORE PR-2 ships
------------------------------

PR-2 adds the actual ``tear_down_org()`` function to
``tenant_reconciler``. Until then, this CLI imports the function
defensively: if it's missing, the CLI logs a single WARNING per
candidate org explaining that PR-2 isn't shipped yet, then exits
cleanly. This keeps the chart deployable on PR-3 alone — operators
see the cron running + a clear "not yet implemented" log instead of
a CrashLoopBackOff.

Dry-run preview (design §4a)
----------------------------

When ``TENANT_TEARDOWN_DRY_RUN_HOURS`` is set (default 24), every
candidate is logged at INFO with a "would tear down" line on
``deleted_at + grace_days < now <= deleted_at + grace_days +
dry_run_hours`` — giving the operator a window to intervene before
actual deletion. The actual delete fires once ``now >`` that window.

Usage
-----

Designed for ``python -m server.services.tenant_teardown`` invoked
by the cron. No CLI flags — everything reads from environment
variables already set by the Helm chart's CronJob template.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)


# Operator-tunable; mirrors the env vars the Helm CronJob sets.
_GRACE_DAYS = int(os.environ.get("TENANT_DELETION_GRACE_DAYS", "30"))
_DRY_RUN_HOURS = int(os.environ.get("TENANT_TEARDOWN_DRY_RUN_HOURS", "24"))


def _utcnow() -> datetime:
    """Naive UTC. Matches the rest of the codebase's datetime style."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _candidate_orgs(db, now: datetime) -> list:
    """Return orgs whose grace period has elapsed.

    Imported inline so this module can be imported under tests that
    don't have the full ORM available.
    """
    from ..database.models import Organization

    cutoff = now - timedelta(days=_GRACE_DAYS)
    stmt = (
        select(Organization)
        .where(Organization.deleted_at.isnot(None))
        .where(Organization.deleted_at < cutoff)
        .order_by(Organization.deleted_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars())


async def _is_in_dry_run_window(org, now: datetime) -> bool:
    """True iff the org is in the dry-run window: past the grace
    cutoff but not yet past the cutoff + dry-run buffer."""
    if _DRY_RUN_HOURS <= 0:
        return False
    if org.deleted_at is None:
        return False
    cutoff = org.deleted_at + timedelta(days=_GRACE_DAYS)
    dry_end = cutoff + timedelta(hours=_DRY_RUN_HOURS)
    return cutoff < now <= dry_end


async def _tear_down_org_safe(db, org) -> None:
    """Wrap the reconciler's destructor in the failure-safe contract.

    PR-2 ships ``tenant_reconciler.tear_down_org``. Until then, log a
    one-line warning and move on — the cron keeps running and the
    operator's logs show exactly what's pending.
    """
    from . import tenant_reconciler  # local import to avoid module-load failures

    teardown_fn = getattr(tenant_reconciler, "tear_down_org", None)
    if teardown_fn is None:
        logger.warning(
            "tenant_reconciler.tear_down_org is not implemented yet "
            "(lands in Epic #35 #36 PR-2 / K8s+S3+Vault layer); "
            "org %s would be torn down once PR-2 ships",
            org.id,
        )
        return

    try:
        # PR-2's implementation may be sync or async; await if needed.
        result = teardown_fn(db, org)
        if asyncio.iscoroutine(result):
            await result
        logger.info("torn down org %s (deleted_at=%s)", org.id, org.deleted_at)
    except Exception as exc:
        # Failure-safe: record + continue. PR-2's reconciler also
        # writes ``tenant_states.reconcile_error`` on failure; this
        # log line is the cron-side breadcrumb so the operator sees
        # the failure in the Kubernetes pod log too.
        logger.warning(
            "tear-down failed for org %s; will retry next cron tick: %s",
            org.id, exc, exc_info=True,
        )


async def _run() -> int:
    """Main entry-point. Always returns 0; failures are logged."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL is not set; refusing to run")
        return 0

    if os.environ.get("TENANT_ISOLATION_ENABLED", "").lower() not in (
        "true", "1", "yes",
    ):
        logger.info(
            "TENANT_ISOLATION_ENABLED is not true; nothing to do",
        )
        return 0

    # Mirror the web pod's async-engine setup. expire_on_commit=False
    # so we can read attributes after the implicit transaction closes.
    engine = create_async_engine(database_url, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    now = _utcnow()
    try:
        async with SessionLocal() as db:
            candidates = await _candidate_orgs(db, now)
            logger.info(
                "tenant tear-down sweep: %d candidates "
                "(grace=%d days, dry_run=%d hours)",
                len(candidates), _GRACE_DAYS, _DRY_RUN_HOURS,
            )
            for org in candidates:
                if await _is_in_dry_run_window(org, now):
                    # Log only — operator's window to intervene.
                    logger.info(
                        "DRY-RUN: would tear down org %s "
                        "(deleted_at=%s, grace=%d days, dry_run=%d hours); "
                        "actual delete after dry-run window expires",
                        org.id, org.deleted_at, _GRACE_DAYS, _DRY_RUN_HOURS,
                    )
                    continue
                await _tear_down_org_safe(db, org)
            await db.commit()
    finally:
        await engine.dispose()

    return 0


def main() -> int:
    """Sync entry-point for ``python -m server.services.tenant_teardown``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
