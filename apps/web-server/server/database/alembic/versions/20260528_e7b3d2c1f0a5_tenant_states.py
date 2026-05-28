"""add tenant_states table + Organization.tenant_namespace + Organization.deleted_at

Epic #35 #36 PR-1. Schema groundwork for Tenant Isolation Mode
(per-tenant K8s Namespace + per-tenant ServiceAccount + per-tenant
NetworkPolicy + per-tenant S3 prefix + per-tenant Vault path).

The reconciler that ACTS on this schema is intentionally a dry-run
in PR-1 — logs the decisions it would take, but does NOT call the
Kubernetes / IAM / Vault APIs. PR-2 adds the real K8s/IAM/Vault
writes.

Adds:

- ``tenant_states`` — one row per Organization tracking the
  reconciler's view of K8s/cloud resource state.
- ``organizations.tenant_namespace`` — the immutable K8s namespace
  name for the org (computed once at create-time per design
  decision #2).
- ``organizations.deleted_at`` — soft-delete timestamp; the
  reconciler reads this to stage tear-down per design decision #7
  (30-day grace).

Backward compat: every existing Organization gets ``tenant_state``
row on first reconcile pass with ``isolation_mode='shared'``
(legacy behavior, no per-tenant resources). ``tenant_namespace``
left NULL until the operator flips ``tenant.isolationEnabled=true``.

Revision ID: e7b3d2c1f0a5
Revises: d4f8c1a3e2b7
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7b3d2c1f0a5"
down_revision: str | Sequence[str] | None = "d4f8c1a3e2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # tenant_states — one row per Organization (PK = org_id FK).
    op.create_table(
        "tenant_states",
        sa.Column(
            "org_id", sa.String(length=36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "isolation_mode", sa.String(length=16),
            nullable=False, server_default="shared",
        ),
        sa.Column("namespace_name", sa.String(length=63), nullable=True),
        sa.Column("service_account", sa.String(length=63), nullable=True),
        sa.Column("iam_role_arn", sa.String(length=2048), nullable=True),
        sa.Column("vault_policy_name", sa.String(length=255), nullable=True),
        sa.Column("reconciled_at", sa.DateTime, nullable=True),
        sa.Column("reconcile_error", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime,
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime,
            nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_tenant_states_isolation_mode",
        "tenant_states", ["isolation_mode"],
    )
    # Index on reconcile_error for the SQL-queryable health check
    # operators run: SELECT org_id, reconcile_error FROM tenant_states
    # WHERE reconcile_error IS NOT NULL.
    op.create_index(
        "ix_tenant_states_reconcile_error",
        "tenant_states", ["reconcile_error"],
        postgresql_where=sa.text("reconcile_error IS NOT NULL"),
    )

    # Organization.tenant_namespace (nullable; computed on first
    # reconcile when isolation is enabled).
    with op.batch_alter_table("organizations") as batch:
        batch.add_column(
            sa.Column(
                "tenant_namespace", sa.String(length=63), nullable=True,
            ),
        )
        # Organization.deleted_at (nullable; set by org-soft-delete).
        batch.add_column(
            sa.Column("deleted_at", sa.DateTime, nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("organizations") as batch:
        batch.drop_column("deleted_at")
        batch.drop_column("tenant_namespace")

    op.drop_index(
        "ix_tenant_states_reconcile_error", table_name="tenant_states",
    )
    op.drop_index(
        "ix_tenant_states_isolation_mode", table_name="tenant_states",
    )
    op.drop_table("tenant_states")
