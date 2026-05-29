"""per-tenant audit-chain anchor — Option A (v1.2 #208)

Adds schema changes required for per-tenant audit chains:

- ``audit_anchors.org_id`` — nullable FK to organizations.id (ON DELETE SET
  NULL); NULL means shared deployment-wide chain (v1.1 backward compat).
  Chain preservation on org delete is intentional: the chain stays as a
  legal-hold artefact even after the org row is deleted.

- ``audit_signing_keys.org_id`` — nullable FK to organizations.id (ON DELETE
  CASCADE); NULL means the deployment-wide signing key from v1.1. Per-tenant
  keys are CASCADE-deleted only when the org hard-deletes; before that the key
  row stays so historical anchors remain verifiable.

- ``tenant_audit_state`` — one row per isolated org, tracking the per-tenant
  chain head, cutover boundary, and lifecycle ('active' | 'sealed').

- Composite index ``audit_logs(org_id, created_at)`` — hot-path for the
  ``_load_tenant_chain_head_locked`` query that finds the latest row per org
  (recommendation #2 in the v1.2 design doc).

- Partial unique index on ``audit_signing_keys(org_id) WHERE retired_at IS
  NULL`` — at most one active key per tenant (Postgres only; SQLite guard is
  application-side per design decision §1).

- Replace the v1.1 ``uq_audit_anchors_date`` per-day unique constraint with a
  per-(org, day) one that also handles NULL org_id correctly via COALESCE
  (design decision §10).

Revision ID: c3d7e8f1a2b4
Revises: a1b2c3d4e5f6
Create Date: 2026-05-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d7e8f1a2b4"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Add audit_anchors.org_id — nullable FK, ON DELETE SET NULL.
    # Chain preservation on org delete is locked in the v1.2 design:
    # the anchor stays so historical verification still works after the
    # org row is gone.
    with op.batch_alter_table("audit_anchors") as batch:
        batch.add_column(
            sa.Column(
                "org_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )

    op.create_index(
        "ix_audit_anchors_org_signed_at",
        "audit_anchors",
        ["org_id", "signed_at"],
    )

    # 2. Drop the v1.1 single-date unique index; replace with the
    # (org_id, day) unique constraint (design decision §10).
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_audit_anchors_date")
        # COALESCE maps NULL org_id to a fixed sentinel so Postgres treats
        # two shared-chain anchors on the same day as duplicates (Postgres
        # ordinarily treats NULL != NULL, which would allow multiple NULL
        # rows on the same day).
        op.execute(
            "CREATE UNIQUE INDEX ux_audit_anchors_org_day "
            "ON audit_anchors ("
            "COALESCE(org_id, '00000000-0000-0000-0000-000000000000'), "
            "(signed_at::date)"
            ")"
        )
    # SQLite test paths: application-side guard (idempotency via
    # _existing_anchor_for_day + unique constraint on (org_id, signed_at::date)
    # is not expressible in portable SQL; the cron's try/except
    # catches IntegrityError).

    # 3. Add audit_signing_keys.org_id — nullable FK, ON DELETE CASCADE.
    # Per-tenant key is CASCADE-deleted only when the org hard-deletes;
    # this is the legal-hold boundary: the key stays while the org row
    # stays, so all historical anchors remain verifiable.
    with op.batch_alter_table("audit_signing_keys") as batch:
        batch.add_column(
            sa.Column(
                "org_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )

    # Partial unique index: at most one active key per org (Postgres only).
    # "active" = retired_at IS NULL. The deployment-wide key (org_id IS NULL)
    # continues to have its own active-row uniqueness enforced application-side
    # (unchanged from v1.1 — the cron calls ensure_active_key which selects
    # MAX(version) WHERE retired_at IS NULL AND org_id IS NULL).
    if dialect == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX ux_audit_signing_keys_org_active "
            "ON audit_signing_keys (org_id) "
            "WHERE retired_at IS NULL AND org_id IS NOT NULL"
        )

    # 4. Composite index on audit_logs(org_id, created_at) — hot-path for
    # the per-tenant chain-head query (design recommendation #2).
    op.create_index(
        "ix_audit_logs_org_id_created_at",
        "audit_logs",
        ["org_id", "created_at"],
    )

    # 5. Create tenant_audit_state table (design decision §2).
    # Separate from tenant_states because:
    # (a) audit cron writes at every audit_logs INSERT (high cadence),
    # (b) reconciler writes every 5-min sweep (lower cadence, different lock
    #     pattern). Coupling them would force the reconciler to handle audit
    #     locking.
    op.create_table(
        "tenant_audit_state",
        sa.Column(
            "org_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # The created_at of the FIRST audit row written under the per-tenant
        # chain (i.e. after the isolation flip). Verifiers use this to split
        # pre-cutover (shared chain) from post-cutover (per-tenant chain).
        sa.Column("chain_started_at", sa.DateTime, nullable=False),
        # Hex SHA-256 of the current per-tenant chain head. Updated in the
        # same transaction as each audit_logs INSERT for that org (design
        # decision §8 SELECT ... FOR UPDATE).
        sa.Column("current_head_hash", sa.String(64), nullable=False),
        # NULL before the first daily anchor; non-NULL after.
        sa.Column("last_anchor_at", sa.DateTime, nullable=True),
        # 'active': new rows can be appended.
        # 'sealed': org soft-deleted; chain frozen for legal-hold.
        sa.Column(
            "lifecycle",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_table("tenant_audit_state")

    op.drop_index("ix_audit_logs_org_id_created_at", table_name="audit_logs")

    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ux_audit_signing_keys_org_active")

    with op.batch_alter_table("audit_signing_keys") as batch:
        batch.drop_column("org_id")

    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ux_audit_anchors_org_day")
        # Restore the v1.1 single-date unique index on shared chain.
        op.execute(
            "CREATE UNIQUE INDEX uq_audit_anchors_date "
            "ON audit_anchors ((signed_at::date))"
        )

    op.drop_index("ix_audit_anchors_org_signed_at", table_name="audit_anchors")

    with op.batch_alter_table("audit_anchors") as batch:
        batch.drop_column("org_id")
