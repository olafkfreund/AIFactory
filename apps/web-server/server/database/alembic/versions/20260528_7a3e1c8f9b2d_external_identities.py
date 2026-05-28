"""add external_identities table for multi-IdP linkage

Epic #35 #41 PR-1b. Replaces the single-column ``users.oidc_sub``
pattern with a generic many-to-many surface: one ``external_identities``
row per (user, IdP-kind, subject) tuple. This lets the same user
have:

- one OIDC identity (Okta or Auth0 or GitHub-OIDC)
- one SAML identity (a corp IdP)
- in v1.2+, multiple OIDC providers, etc.

Cross-IdP collision guard (design decision #4) is enforced at the
application layer (saml/routes.py): an incoming SAML assertion for
``alice@corp.com`` is rejected with 409 if a row already exists for
that user with a DIFFERENT ``kind`` and the operator hasn't manually
linked the identities.

``users.oidc_sub`` stays for backward compat — the upgrade backfills
existing OIDC users into ``external_identities`` so the new lookup
path returns identical results. PR-1b's SAML routes use only the new
table; the existing OIDC code keeps using ``oidc_sub`` for now and
migrates to the new table in a follow-up.

Revision ID: 7a3e1c8f9b2d
Revises: b2d4f7e9c3a1
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a3e1c8f9b2d"
down_revision: str | Sequence[str] | None = "b2d4f7e9c3a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create the new table.
    op.create_table(
        "external_identities",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ``kind`` is the IdP type: 'oidc:<provider>' or 'saml:<idp-name>'.
        # We keep the prefix structure deliberately so a future query
        # can `LIKE 'oidc:%'` to find all OIDC identities.
        sa.Column("kind", sa.String(length=64), nullable=False),
        # The IdP-side subject (NameID for SAML, sub claim for OIDC).
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        # (kind, subject) is globally unique — the IdP guarantees uniqueness
        # within its own kind, and we prevent collisions across IdP kinds
        # by checking application-side before insert.
        sa.UniqueConstraint(
            "kind", "subject", name="uq_external_identities_kind_subject",
        ),
        # A user can only have ONE identity per kind+subject pair, but
        # may have multiple identities of the same kind (e.g. several
        # OIDC providers). This is implied by the FK + unique above.
    )
    op.create_index(
        "ix_external_identities_user_id",
        "external_identities", ["user_id"],
    )
    op.create_index(
        "ix_external_identities_kind",
        "external_identities", ["kind"],
    )

    # Backfill: copy existing users.oidc_sub into external_identities so
    # the new lookup path returns the same set of identities the old
    # code did. We use 'oidc:legacy' as the kind because pre-#41 OIDC
    # code didn't distinguish providers — they all wrote to one column.
    #
    # ``op.execute`` runs the SQL directly so we don't need to import
    # the ORM model. UUID generation differs per DB: Postgres has
    # gen_random_uuid(), SQLite needs a Python-side generator. We
    # generate them in Python for portability.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, oidc_sub FROM users WHERE oidc_sub IS NOT NULL")
    ).fetchall()
    if rows:
        import uuid as _uuid
        conn.execute(
            sa.text("""
                INSERT INTO external_identities (id, user_id, kind, subject)
                VALUES (:id, :user_id, :kind, :subject)
            """),
            [
                {
                    "id": str(_uuid.uuid4()),
                    "user_id": r[0],
                    "kind": "oidc:legacy",
                    "subject": r[1],
                }
                for r in rows
            ],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_external_identities_kind", table_name="external_identities",
    )
    op.drop_index(
        "ix_external_identities_user_id", table_name="external_identities",
    )
    op.drop_table("external_identities")
