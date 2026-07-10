"""add job_states table (RFC-0016 #668 durable job state)

Phase 1 of RFC-0016 horizontal concurrency. Replaces AIFactory's per-pod
in-memory ``running_tasks`` dict + FIFO ``QueuedTask`` deque (in
``services/agent_service.py``) with a durable, multi-replica-safe row per
job conforming to ``apis/job-state.schema.json`` in the Factory hub.

Adds:

- ``job_states`` — one row per in-flight or completed build. ``job_id``
  (the AIFactory ``task_id`` = ``project_id:spec_id``) is the PK so two
  control-plane replicas can never double-start the same job, and the
  admission cap/queue read live counts from this table inside a
  ``SELECT ... FOR UPDATE`` transaction (see ``services/job_state_store.py``).

Backward compat: when ``DATABASE_URL`` is unset the service keeps the
in-memory cap/queue (single-pod dev) and logs that it is not
multi-replica safe — this table is simply unused in that mode.

Revision ID: b7e1c9a4d2f3
Revises: f1a2b3c4d5e6
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e1c9a4d2f3"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_states",
        sa.Column(
            "schema_version",
            sa.String(length=8),
            nullable=False,
            server_default="1",
        ),
        sa.Column("job_id", sa.String(length=255), primary_key=True),
        sa.Column("correlation_key", sa.String(length=255), nullable=True),
        sa.Column(
            "service",
            sa.String(length=16),
            nullable=False,
            server_default="aifactory",
        ),
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="build",
        ),
        sa.Column("lifecycle_state", sa.String(length=16), nullable=False),
        sa.Column("service_status", sa.String(length=64), nullable=True),
        sa.Column("phase", sa.String(length=64), nullable=True),
        sa.Column(
            "attempt",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
        sa.Column("admission", sa.JSON, nullable=True),
        sa.Column("worker_ref", sa.JSON, nullable=True),
        sa.Column("spawn_args", sa.JSON, nullable=True),
        sa.Column("result", sa.JSON, nullable=True),
        sa.Column("usage", sa.JSON, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
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
        sa.Column("ended_at", sa.DateTime, nullable=True),
    )
    # The admission cap counts active rows for one service; index the filter
    # columns so the FOR UPDATE count stays cheap as terminal history grows.
    op.create_index(
        "ix_job_states_service_lifecycle",
        "job_states",
        ["service", "lifecycle_state"],
    )
    op.create_index(
        "ix_job_states_correlation_key",
        "job_states",
        ["correlation_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_job_states_correlation_key",
        table_name="job_states",
    )
    op.drop_index(
        "ix_job_states_service_lifecycle",
        table_name="job_states",
    )
    op.drop_table("job_states")
