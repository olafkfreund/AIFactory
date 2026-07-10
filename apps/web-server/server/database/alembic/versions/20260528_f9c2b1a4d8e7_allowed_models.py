"""add organizations.allowed_models JSON column

Epic #35 #38 PR-2a. Schema groundwork for per-org model allowlist
enforcement via the LiteLLM gateway. The reconciler that ACTS on
this column (calling LiteLLM's admin API to update per-tenant
virtual-key model lists) ships in PR-2b along with the audit hook
+ PII redactor.

Adds:

- ``organizations.allowed_models`` — JSON array of model name
  patterns the org may use. ``["*"]`` (default) = all models
  allowed (backward compat for pre-#38 deployments). Concrete
  examples:
  - ``["claude-*"]`` — Anthropic family only
  - ``["gpt-4o-mini", "gpt-4o"]`` — specific OpenAI models
  - ``["bedrock/anthropic.*"]`` — Bedrock-routed Anthropic only

The JSON type maps to JSONB on Postgres + TEXT (with JSON1) on
SQLite, so the column works on both DBs without backend-specific
SQL. The Python-side serialization is automatic via SQLAlchemy.

The ``["*"]`` default is critical: existing orgs reading this
column post-migration get the "all allowed" sentinel, so no
existing LLM call breaks. Only when an operator explicitly narrows
``allowed_models`` does enforcement kick in.

Revision ID: f9c2b1a4d8e7
Revises: e7b3d2c1f0a5
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9c2b1a4d8e7"
down_revision: str | Sequence[str] | None = "e7b3d2c1f0a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # JSON column with server_default of '["*"]' (the all-allowed sentinel).
    # Postgres stores as JSONB; SQLite stores as TEXT validated by JSON1.
    # batch_alter_table for SQLite ALTER TABLE compatibility.
    with op.batch_alter_table("organizations") as batch:
        batch.add_column(
            sa.Column(
                "allowed_models",
                sa.JSON,
                nullable=False,
                server_default='["*"]',
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("organizations") as batch:
        batch.drop_column("allowed_models")
