"""
SQLAlchemy ORM models for the AIFactory multi-user system.

All models use SQLAlchemy 2.x declarative style with Mapped columns.
UUIDs are stored as strings since SQLite lacks native UUID support.
Timestamps use server-side defaults via ``func.now()``.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Re-export under a private alias so the model definitions read cleanly
# while making it obvious this is the encrypted-at-rest column type
# (Epic #26 P2). See apps/web-server/server/crypto/.
from ..crypto.encrypted_string import EncryptedString as _EncryptedString


def _generate_uuid() -> str:
    """Generate a new UUID4 string for use as a primary key."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    pass


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class User(Base):
    """Application user account."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Epic #26 P3.3 — Stable OIDC subject identifier. Set on first
    # successful OIDC login (JIT-provisioned). Nullable so that
    # locally-registered users (no SSO) don't need it; unique so that
    # the same IdP user can't accidentally collide across logins.
    oidc_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    # Epic #26 P5.5 — GDPR right-to-erasure timestamp. When set, PII
    # columns (email, name, OAuth tokens) MUST be NULL. Used by the
    # admin UI to render "Erased on YYYY-MM-DD" placeholders instead
    # of treating the user row as deleted. The audit chain preserves
    # historical user_id references via SHA-256 hashing.
    gdpr_erased_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    owned_organizations: Mapped[list["Organization"]] = relationship(
        "Organization",
        back_populates="owner",
        foreign_keys="Organization.owner_id",
    )
    org_memberships: Mapped[list["OrgMember"]] = relationship(
        "OrgMember",
        back_populates="user",
        foreign_keys="OrgMember.user_id",
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(
        "ApiKey", back_populates="user"
    )
    # Epic #35 #41 PR-1b — per-IdP identity records. One row per
    # (kind, subject) pair the user has logged in with.
    external_identities: Mapped[list["ExternalIdentity"]] = relationship(
        "ExternalIdentity", back_populates="user",
        cascade="all, delete-orphan",
    )
    # Epic #35 #43 PR-1 — last successful auth timestamp. Updated by
    # auth.py on every OIDC/SAML/password login. NULL = never logged in.
    # Used by /api/admin/access-review (SOC2 CC6.2 / ISO 27001 A.9.2.5).
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )

    def __repr__(self) -> str:
        return f"<User id={self.id!r} email={self.email!r}>"


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


class Organization(Base):
    """Organization (team/workspace) that owns projects."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    settings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    owner: Mapped["User"] = relationship(
        "User",
        back_populates="owned_organizations",
        foreign_keys=[owner_id],
    )
    members: Mapped[list["OrgMember"]] = relationship(
        "OrgMember", back_populates="organization"
    )
    projects: Mapped[list["Project"]] = relationship(
        "Project", back_populates="organization"
    )
    api_keys: Mapped[list["ApiKey"]] = relationship(
        "ApiKey", back_populates="organization"
    )

    def __repr__(self) -> str:
        return f"<Organization id={self.id!r} slug={self.slug!r}>"


# ---------------------------------------------------------------------------
# Organization Members (join table with role)
# ---------------------------------------------------------------------------


class OrgMember(Base):
    """Membership linking a user to an organization with a specific role."""

    __tablename__ = "org_members"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_org_members_org_user"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        String(50), nullable=False, default="member"
    )  # owner | admin | member | viewer
    invited_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="members"
    )
    user: Mapped["User"] = relationship(
        "User",
        back_populates="org_memberships",
        foreign_keys=[user_id],
    )
    inviter: Mapped["User | None"] = relationship(
        "User", foreign_keys=[invited_by]
    )

    def __repr__(self) -> str:
        return (
            f"<OrgMember org_id={self.org_id!r} "
            f"user_id={self.user_id!r} role={self.role!r}>"
        )


# ---------------------------------------------------------------------------
# OIDC Refresh Sessions (Epic #26 P3.4)
# ---------------------------------------------------------------------------


class OidcRefreshSession(Base):
    """Per-refresh-token session for OIDC-authenticated users.

    Created when a user completes OIDC login (P3.1) and tracks the
    refresh path's IdP revalidation cadence (P3.4). The row is deleted
    when the user logs out (P3.5) or when the IdP rejects a refresh
    (revocation propagation).

    The refresh token itself is NOT stored — only its ``jti`` claim,
    which lets refresh lookups find the session without exposing the
    bearer secret to anyone with DB read access.
    """

    __tablename__ = "oidc_refresh_sessions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    jti: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    oidc_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_validated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<OidcRefreshSession user_id={self.user_id!r} "
            f"jti={self.jti[:8]!r}... sub={self.oidc_sub!r}>"
        )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class Project(Base):
    """A project managed within an organization."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="projects"
    )
    creator: Mapped["User | None"] = relationship("User", foreign_keys=[created_by])
    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="project")

    def __repr__(self) -> str:
        return f"<Project id={self.id!r} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class Task(Base):
    """A task (spec) belonging to a project."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="backlog"
    )
    spec_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    assigned_to: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    project: Mapped["Project"] = relationship("Project", back_populates="tasks")
    creator: Mapped["User | None"] = relationship(
        "User", foreign_keys=[created_by]
    )
    assignee: Mapped["User | None"] = relationship(
        "User", foreign_keys=[assigned_to]
    )

    def __repr__(self) -> str:
        return f"<Task id={self.id!r} title={self.title!r} status={self.status!r}>"


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------


class ApiKey(Base):
    """API key for programmatic access, scoped to a user and organization."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="api_keys")
    organization: Mapped["Organization"] = relationship(
        "Organization", back_populates="api_keys"
    )

    def __repr__(self) -> str:
        return f"<ApiKey id={self.id!r} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Git Credentials (encrypted PATs for cloning private repos — epic #82 PR-C)
# ---------------------------------------------------------------------------


class GitCredential(Base):
    """Stored Git credential for the portal-managed clone flow (#82 PR-C).

    V1 supports HTTPS personal-access-token (PAT) credentials only. Deploy
    Keys (SSH) and GitHub App install IDs (short-lived tokens) are out of
    scope for V1 — both are tracked as follow-ups on epic #82.

    The token is encrypted at rest via ``EncryptedString`` (Epic #26 P2).
    Scope is **per-org** rather than per-user: anyone with rights on the
    org can use the credential to clone — matches how teams typically
    share Deploy Keys today.

    Per-project binding happens via ``ProjectCreate.gitCredentialId``
    (already accepted by the API since PR-A — wired in this PR-C). The
    credential's ``host`` field is informational only (e.g. ``github.com``,
    ``gitlab.example.internal``); URL matching is the caller's job.
    """

    __tablename__ = "git_credentials"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    # Human-readable label, e.g. "github-deploy-bot" or "gitlab-readonly".
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Credential kind. V1: ``pat`` only. ``deploy_key`` and ``github_app``
    # land in later follow-ups; the enum-by-convention keeps the column
    # forward-compatible without a migration.
    kind: Mapped[str] = mapped_column(String(50), nullable=False, default="pat")
    # Informational host (no enforcement) — surfaces in the UI so users
    # can tell which credential applies to which project.
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # GitHub PATs prefer the ``oauth2`` username; GitLab PATs use ``oauth2``
    # too. Empty/None means "username portion not needed" (rare).
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The actual token — never logged, never returned via API after creation.
    token: Mapped[str] = mapped_column(_EncryptedString(), nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")

    def __repr__(self) -> str:
        return f"<GitCredential id={self.id!r} name={self.name!r}>"


# ---------------------------------------------------------------------------
# Email Accounts (OAuth-connected email for notifications)
# ---------------------------------------------------------------------------


class EmailAccount(Base):
    """OAuth-connected email account for sending notifications."""

    __tablename__ = "email_accounts"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", name="uq_email_accounts_user_provider"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "outlook" | "gmail"
    email_address: Mapped[str] = mapped_column(String(255), nullable=False)
    # P2.3: OAuth credentials encrypted at rest via EncryptedString.
    # See apps/web-server/server/crypto/ for the at-rest encryption layer.
    access_token: Mapped[str] = mapped_column(
        _EncryptedString(), nullable=False
    )
    refresh_token: Mapped[str | None] = mapped_column(
        _EncryptedString(), nullable=True
    )
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        return (
            f"<EmailAccount id={self.id!r} provider={self.provider!r} "
            f"email={self.email_address!r}>"
        )


# ---------------------------------------------------------------------------
# LLM Endpoints (OpenAI-compatible user-defined endpoints)
# ---------------------------------------------------------------------------


class LLMEndpoint(Base):
    """User-defined OpenAI-compatible LLM endpoint (LM Studio, vLLM, OpenRouter, etc.)."""

    __tablename__ = "llm_endpoints"
    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_llm_endpoints_user_label"),
        Index("ix_llm_endpoints_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # P2.3: provider API key encrypted at rest via EncryptedString.
    api_key: Mapped[str | None] = mapped_column(_EncryptedString(), nullable=True)
    default_model: Mapped[str] = mapped_column(String(255), nullable=False)
    headers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        return (
            f"<LLMEndpoint id={self.id!r} label={self.label!r} "
            f"base_url={self.base_url!r}>"
        )


# ---------------------------------------------------------------------------
# Audit Logs
# ---------------------------------------------------------------------------


class AuditLog(Base):
    """Immutable audit trail for security-relevant actions."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_id", "org_id"),
        Index("ix_audit_logs_user_id", "user_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    org_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    # Epic #26 P5.1 — daily retention job deletes rows where
    # retention_until <= now(). Default policy: 13 months (SOC2 12mo +
    # buffer); set per-row at write time so the policy can vary by
    # action class (login events: short, security events: long).
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    # Epic #26 P5.2 — Per-row hash chain. SHA-256 of the previous
    # row's content (or the genesis sentinel for the first row).
    # Threat model: tamper-detection within the audit log only.
    # Signed external anchor lands in Epic #35 #43 (see AuditAnchor).
    prev_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # Epic #35 #43 PR-1 — data-classification tier. One of
    # 'public' | 'internal' | 'confidential'. Included in
    # `_canonical()` so the chain protects classification against
    # tampering (an attacker can't silently flip confidential→public
    # to leak rows past the `?max_classification` export filter).
    # Default is 'internal'; classifiers in audit_service set
    # 'confidential' for KMS access / key rotation / GDPR erasure /
    # audit-chain rewrites.
    classification: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="internal",
    )

    # Relationships (read-only lookups, no back_populates needed)
    organization: Mapped["Organization | None"] = relationship(
        "Organization", foreign_keys=[org_id]
    )
    user: Mapped["User | None"] = relationship(
        "User", foreign_keys=[user_id]
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id!r} action={self.action!r} "
            f"resource_type={self.resource_type!r}>"
        )


# ---------------------------------------------------------------------------
# P2.2 — KMS data keys (per-organization, wrapped by KMS root key)
# ---------------------------------------------------------------------------


class KmsDataKey(Base):
    """Per-organization data key, wrapped by the active KMS root.

    Each organization gets one (and only one) active row. Workflow:
      1. App generates a random 32-byte data key.
      2. The active KMS backend (`crypto.kms.get_backend()`) encrypts the
         data key under the root key, producing `wrapped_key`.
      3. EncryptedString columns scoped to that org are encrypted under
         the data key (P2.3 wires the binding).
      4. KMS root rotation re-wraps `wrapped_key` and bumps `rotated_at`
         so the in-process LRU cache (DataKeyManager) re-fetches (P2.5).
    """

    __tablename__ = "kms_data_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    kms_key_id: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="Identifier of the KMS root key that wrapped this data key. "
                "For fernet backend: literal `fernet:default`. For aws_kms: "
                "the KMS ARN. Lets rotation runbooks know which backend "
                "wrapped each row.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    rotated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
        comment="Updated on every re-wrap (root key rotation). The "
                "DataKeyManager polls this column to invalidate its "
                "in-process LRU cache.",
    )

    def __repr__(self) -> str:
        return (
            f"<KmsDataKey id={self.id!r} org_id={self.org_id!r} "
            f"kms_key_id={self.kms_key_id!r}>"
        )


# ---------------------------------------------------------------------------
# External identities (Epic #35 #41 PR-1b)
# ---------------------------------------------------------------------------


class ExternalIdentity(Base):
    """Per-IdP identity record for a User.

    One row per (user, IdP-kind, subject) tuple. ``kind`` uses a
    structured prefix so future queries can filter all OIDC vs all
    SAML identities:

        'oidc:legacy'      — pre-#41 OIDC users (backfilled by migration)
        'oidc:okta'        — OIDC against Okta
        'oidc:github'      — OIDC against GitHub
        'saml:corp-sso'    — SAML against an IdP the operator named 'corp-sso'

    Cross-IdP collision guard (design decision #4): the SAML routes
    layer rejects with 409 when an incoming SAML assertion's email
    matches a user that already has a DIFFERENT-kind identity. Linking
    is admin-only (out of v1.1 scope).
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint(
            "kind", "subject", name="uq_external_identities_kind_subject",
        ),
        Index("ix_external_identities_user_id", "user_id"),
        Index("ix_external_identities_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship(
        "User", back_populates="external_identities",
    )

    def __repr__(self) -> str:
        return (
            f"<ExternalIdentity user_id={self.user_id!r} kind={self.kind!r}>"
        )


# ---------------------------------------------------------------------------
# Audit anchor + signing key (Epic #35 #43 PR-1)
# ---------------------------------------------------------------------------


class AuditSigningKey(Base):
    """Versioned wrapped-key storage for the audit-chain anchor signer.

    Why a table (not a single env-var key): KMS root-key rotation
    requires re-wrapping. Storing every wrapped version means anchor
    rows with ``key_version=N`` stay verifiable forever — the verifier
    loads ``audit_signing_keys[N].wrapped_key``, unwraps via the
    current KMS root, and verifies the HMAC.

    Operational rules:
      - INSERT a new row when generating a new signing key.
      - Set ``retired_at`` on the previous row at the same time.
      - The signer always uses ``MAX(version) WHERE retired_at IS NULL``.
      - Never DELETE a row — older anchors need their key forever.
    """

    __tablename__ = "audit_signing_keys"

    version: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True,
    )
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )

    def __repr__(self) -> str:
        # NEVER include wrapped_key here — it's encrypted but operational
        # discipline says we don't even render its length in logs.
        retired = self.retired_at.isoformat() if self.retired_at else "active"
        return f"<AuditSigningKey v{self.version} {retired}>"


class AuditAnchor(Base):
    """One signed snapshot of the audit chain head.

    Daily cron emits one row at 00:00 UTC. The export endpoint
    interleaves these into the NDJSON stream so external verifiers
    can prove untamperedness by re-computing the chain + verifying
    the HMAC.

    Append-only at the application layer (no PATCH / DELETE routes).
    """

    __tablename__ = "audit_anchors"
    __table_args__ = (
        Index("ix_audit_anchors_signed_at", "signed_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid,
    )
    # Hex SHA-256 of the canonical content of the last audit_logs row
    # whose created_at < the anchor's day boundary. Empty (=GENESIS)
    # for an anchor emitted before any rows exist.
    chain_head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hex HMAC-SHA256(signing_key, chain_head_hash).
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    key_version: Mapped[int] = mapped_column(
        ForeignKey("audit_signing_keys.version"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )

    signing_key: Mapped["AuditSigningKey"] = relationship(
        "AuditSigningKey", foreign_keys=[key_version],
    )

    def __repr__(self) -> str:
        return (
            f"<AuditAnchor signed_at={self.signed_at!r} "
            f"v{self.key_version}>"
        )
