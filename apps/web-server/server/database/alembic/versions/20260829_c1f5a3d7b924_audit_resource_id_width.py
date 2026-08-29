"""audit_logs.resource_id holds any resource's id, not a UUID

Revision ID: c1f5a3d7b924
Revises: b7e1c9a4d2f3
Create Date: 2026-08-29

Ported from PFactory's ``b8e1f4c7a2d9`` (2026-08-19), which fixed the identical
column there; the fix never propagated to this service (AIFactory#1458).

``resource_id`` was declared ``String(36)`` -- exactly a UUID's length, copied
from the ``id`` columns beside it. But it carries NO foreign key and sits next
to ``resource_type: String(255)``: it is a free-form pointer to a row in
whichever table ``resource_type`` names, and those tables do not all use UUID
keys.

The task pipeline builds a composite id, ``"{project_id}:{spec-slug}"``, which
is 53+ characters. Every audited task action therefore failed with
StringDataRightTruncationError -- and ``audit_service.log_audit_event_bg``
catches that and logs it at WARNING, so the API returned success while no row
was written.

Unlike PFactory, where the table was empty and the emptiness was itself the
evidence, this table holds rows: it looks populated while an entire CLASS of
action -- every task action -- is absent. Given ``audit_logs`` carries
``prev_hash``/``entry_hash``, a silently incomplete chain is a compliance
problem, not just a logging one.

Widening to 255 matches ``resource_type`` and is a pure relaxation -- no
existing value can fail to fit, so the upgrade is safe on a live table. The
downgrade is only safe while every stored value is short enough, which it
asserts rather than assumes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1f5a3d7b924"
down_revision: str | Sequence[str] | None = "b7e1c9a4d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table for SQLite portability -- SQLite has no ALTER COLUMN, so
    # a bare op.alter_column is a syntax error there while being fine on
    # Postgres. The test suite migrates against SQLite too. Same pattern as
    # c6e3b2d4a8f0.
    with op.batch_alter_table("audit_logs") as batch:
        batch.alter_column(
            "resource_id",
            existing_type=sa.String(36),
            type_=sa.String(255),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Refuse rather than truncate: narrowing silently destroys audit references.
    conn = op.get_bind()
    too_long = conn.execute(
        sa.text("SELECT count(*) FROM audit_logs WHERE char_length(resource_id) > 36")
    ).scalar_one()
    if too_long:
        raise RuntimeError(
            f"{too_long} audit_logs row(s) have resource_id longer than 36 chars; "
            "narrowing would truncate audit references. Resolve those rows first."
        )
    with op.batch_alter_table("audit_logs") as batch:
        batch.alter_column(
            "resource_id",
            existing_type=sa.String(255),
            type_=sa.String(36),
            existing_nullable=True,
        )
