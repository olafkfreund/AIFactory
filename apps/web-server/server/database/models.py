"""
SQLAlchemy ORM models for the AIFactory multi-user system.

All models use SQLAlchemy 2.x declarative style with Mapped columns.
UUIDs are stored as strings since SQLite lacks native UUID support.
Timestamps use server-side defaults via ``func.now()``.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
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
    # Epic #35 #36 PR-1 — immutable per-tenant K8s namespace name.
    # NULL until isolation is enabled + first reconcile pass runs;
    # locked once set (slug renames do NOT change this).
    tenant_namespace: Mapped[str | None] = mapped_column(
        String(63), nullable=True,
    )
    # Epic #35 #36 PR-1 — soft-delete timestamp. Stage-1 sets this
    # (immediate PII scrub); stage-2 (day 30) tears down infra.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    # Epic #35 #38 PR-2a — per-org LLM model allowlist. JSON array of
    # model name patterns the org may use. ``["*"]`` (default) means
    # all models allowed (backward compat). Concrete examples:
    # ``["claude-*"]``, ``["gpt-4o-mini", "gpt-4o"]``,
    # ``["bedrock/anthropic.*"]``. The reconciler (PR-2b) syncs this
    # into LiteLLM's per-tenant virtual-key model list via admin API.
    allowed_models: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, server_default='["*"]', default=list,
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

    Our own refresh JWT is NOT stored — only its ``jti`` claim. The
    *IdP's* refresh token (``idp_refresh_token``) IS stored, encrypted at
    rest, when the IdP issues one (the authorization-code flow's refresh
    token): it powers the real per-user revocation check at refresh time
    (#366). Nullable for legacy rows and IdPs that don't issue refresh
    tokens — those fall back to the discovery-liveness probe.
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
    # IdP-issued refresh token (offline_access), encrypted at rest (#366).
    idp_refresh_token: Mapped[str | None] = mapped_column(
        _EncryptedString(), nullable=True
    )
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
# SCIM Groups (Epic #35 #41 PR-1b3)
# ---------------------------------------------------------------------------


class ScimGroup(Base):
    """Parallel SCIM Group resource — design decision #5 locks this as
    a standalone table for v1.1. Integration with the orgs/roles model
    is deferred to Epic #36 (tenant isolation).

    ``external_id`` is the IdP-side group identifier (Azure AD group
    object ID, Okta group ID, etc.). Nullable — not all IdPs send it.

    ``active`` mirrors the SCIM User active flag pattern: soft-deletes
    set it to False. DELETE on /scim/v2/Groups soft-deletes; GET on a
    soft-deleted group returns 404 so Azure AD's sync sees it as gone.
    """

    __tablename__ = "scim_groups"
    __table_args__ = (
        Index("ix_scim_groups_external_id", "external_id"),
        Index("ix_scim_groups_active", "active"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True, unique=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    members: Mapped[list["ScimGroupMember"]] = relationship(
        "ScimGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<ScimGroup id={self.id!r} display_name={self.display_name!r}>"


class ScimGroupMember(Base):
    """Many-to-many join between ScimGroup and User (by User.id).

    Storing User.id here means deleting a user silently orphans these
    rows; the CASCADE on FK handles cleanup. The ``display`` field is
    informational (the user's display name at the time the IdP sent it)
    — we don't keep it in sync with User.name on purpose: SCIM member
    payloads typically include it, and dropping it would lose the IdP's
    original labelling.
    """

    __tablename__ = "scim_group_members"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "user_id", name="uq_scim_group_members_group_user"
        ),
        Index("ix_scim_group_members_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid
    )
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("scim_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    display: Mapped[str | None] = mapped_column(String(255), nullable=True)

    group: Mapped["ScimGroup"] = relationship(
        "ScimGroup", back_populates="members"
    )

    def __repr__(self) -> str:
        return (
            f"<ScimGroupMember group_id={self.group_id!r} "
            f"user_id={self.user_id!r}>"
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

    v1.2 #208: ``org_id`` is NULL for the deployment-wide key (v1.1
    backward compat) and non-NULL for per-tenant keys. ON DELETE CASCADE
    so the key row is removed when the org hard-deletes (day-30 tear-down
    per #36 PR-3); before that the row stays as a legal-hold artefact.
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
    # v1.2 #208 — per-tenant key scoping. NULL = deployment-wide (v1.1).
    # ON DELETE CASCADE: key is removed when the org row is hard-deleted;
    # the key stays during the 30-day grace period (org.deleted_at is set
    # but the row is not yet deleted) so historical anchors stay verifiable.
    org_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Relationship (read-only; no back_populates needed).
    organization: Mapped["Organization | None"] = relationship(
        "Organization", foreign_keys=[org_id],
    )

    def __repr__(self) -> str:
        # NEVER include wrapped_key here — it's encrypted but operational
        # discipline says we don't even render its length in logs.
        retired = self.retired_at.isoformat() if self.retired_at else "active"
        org = f" org={self.org_id}" if self.org_id else ""
        return f"<AuditSigningKey v{self.version}{org} {retired}>"


class AuditAnchor(Base):
    """One signed snapshot of the audit chain head.

    Daily cron emits one row at 00:00 UTC. The export endpoint
    interleaves these into the NDJSON stream so external verifiers
    can prove untamperedness by re-computing the chain + verifying
    the HMAC.

    Append-only at the application layer (no PATCH / DELETE routes).

    v1.2 #208: ``org_id`` is NULL for the deployment-wide shared chain
    anchor (v1.1 backward compat) and non-NULL for per-tenant anchors.
    ON DELETE SET NULL: if the org row is deleted, the anchor row stays
    (legal-hold — the chain remains verifiable from the archived wrapped
    key in audit_signing_keys) but org_id is NULLed out.
    """

    __tablename__ = "audit_anchors"
    __table_args__ = (
        Index("ix_audit_anchors_signed_at", "signed_at"),
        # Composite index for per-tenant anchor lookup (cron + verifier).
        Index("ix_audit_anchors_org_signed_at", "org_id", "signed_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid,
    )
    # Hex SHA-256 of the canonical content of the last audit_logs row
    # whose created_at < the anchor's day boundary. Empty (=GENESIS or
    # GENESIS-T-<uuid> for per-tenant) for an anchor emitted before any
    # rows exist.
    chain_head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hex HMAC-SHA256(signing_key, anchor_input).
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    key_version: Mapped[int] = mapped_column(
        ForeignKey("audit_signing_keys.version"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    # v1.2 #208 — per-tenant anchor scoping. NULL = shared chain (v1.1).
    # ON DELETE SET NULL: chain artefact is preserved when the org is deleted
    # (legal-hold requirement from design finding #3).
    org_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )

    signing_key: Mapped["AuditSigningKey"] = relationship(
        "AuditSigningKey", foreign_keys=[key_version],
    )
    organization: Mapped["Organization | None"] = relationship(
        "Organization", foreign_keys=[org_id],
    )

    def __repr__(self) -> str:
        org = f" org={self.org_id}" if self.org_id else ""
        return (
            f"<AuditAnchor signed_at={self.signed_at!r} "
            f"v{self.key_version}{org}>"
        )


class TenantAuditState(Base):
    """Per-tenant chain head + lifecycle state (Epic #35 v1.2 #208).

    One row per isolated org (isolation_mode='isolated'). Tracks:
    - The genesis boundary (chain_started_at) separating pre-cutover
      (shared chain) from post-cutover (per-tenant chain) rows.
    - The current chain head (current_head_hash), updated in the same
      DB transaction as each audit_logs INSERT for this org (design §8).
    - The last anchor timestamp for health-check queries.
    - The lifecycle: 'active' (new rows allowed) or 'sealed' (org
      soft-deleted, chain frozen for legal-hold).

    Separate from TenantState to avoid coupling the high-cadence audit
    write path with the lower-cadence reconciler sweep (design §2).
    """

    __tablename__ = "tenant_audit_state"

    org_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # created_at of the first per-tenant-chained row, set when isolation_mode
    # flips to 'isolated'. Verifiers use this to split shared vs tenant rows.
    chain_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Hex SHA-256 of the outgoing hash of the last row written to this
    # tenant's chain. Updated atomically with each audit_logs INSERT.
    current_head_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL before the first daily anchor. Used by health-check queries to
    # detect "key issued but no first anchor yet" (design rec #5).
    last_anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    # 'active': per-tenant chain is live.
    # 'sealed': org soft-deleted; no new rows; chain stays for legal-hold.
    lifecycle: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    organization: Mapped["Organization"] = relationship(
        "Organization", foreign_keys=[org_id],
    )

    def __repr__(self) -> str:
        return (
            f"<TenantAuditState org_id={self.org_id!r} "
            f"lifecycle={self.lifecycle!r}>"
        )


# ---------------------------------------------------------------------------
# Tenant isolation state (Epic #35 #36 PR-1)
# ---------------------------------------------------------------------------


class TenantState(Base):
    """Reconciler's view of per-tenant K8s + cloud resource state.

    One row per Organization. The TenantReconciler (in
    ``services/tenant_reconciler.py``) reads this on every reconcile
    pass to decide what to create/update/teardown.

    ``isolation_mode`` enum:
      - ``shared`` — org uses the deployment-default namespace (legacy
        v1.0 mode, byte-for-byte unchanged from pre-#36 deployments)
      - ``isolated`` — org has its own namespace + SA + NetPol + S3
        prefix + Vault path
      - ``deleted`` — org soft-deleted; agent spawner refuses new tasks;
        reconciler tears down resources at day-30 (per
        ``tenant.deletionGraceDays``)

    Operators query ``reconcile_error`` for the health-check pattern:
    ``SELECT org_id, reconcile_error FROM tenant_states WHERE
    reconcile_error IS NOT NULL``.
    """

    __tablename__ = "tenant_states"
    __table_args__ = (
        Index("ix_tenant_states_isolation_mode", "isolation_mode"),
    )

    org_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    isolation_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="shared",
    )
    namespace_name: Mapped[str | None] = mapped_column(
        String(63), nullable=True,
    )
    service_account: Mapped[str | None] = mapped_column(
        String(63), nullable=True,
    )
    iam_role_arn: Mapped[str | None] = mapped_column(
        String(2048), nullable=True,
    )
    vault_policy_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    reconcile_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    organization: Mapped["Organization"] = relationship(
        "Organization", foreign_keys=[org_id],
    )

    def __repr__(self) -> str:
        return (
            f"<TenantState org_id={self.org_id!r} "
            f"mode={self.isolation_mode!r}>"
        )


# ---------------------------------------------------------------------------
# Job state (RFC-0016 #668) — durable, multi-replica-safe build state
# ---------------------------------------------------------------------------


class JobState(Base):
    """One row per in-flight or completed PARR job (RFC-0016 Phase 1).

    Conforms to ``apis/job-state.schema.json`` in the Factory hub. For
    AIFactory every row is ``service='aifactory'`` and ``kind='build'``.
    This table replaces the per-pod in-memory ``running_tasks`` dict + FIFO
    ``QueuedTask`` deque in ``services/agent_service.py`` so that:

      * the admission cap + queue survive a web-server restart, and
      * two control-plane replicas reading the same Postgres cannot exceed
        ``MAX_CONCURRENT_TASKS`` or double-start the same ``job_id`` (the
        slot grant runs inside a ``SELECT ... FOR UPDATE`` transaction —
        see ``services/job_state_store.py``).

    ``job_id`` is the AIFactory ``task_id`` (``project_id:spec_id``).
    ``correlation_key`` threads the upstream GitHub issue
    (``requirements.json -> provenance.issue_number``, #612) across the
    fleet. Large payloads (workspaces, diffs) are NEVER inlined here — only
    references / small terminal result dicts.
    """

    __tablename__ = "job_states"
    __table_args__ = (
        # The admission cap counts active rows for one service; index the
        # filter columns so the FOR UPDATE count stays cheap as history grows.
        Index("ix_job_states_service_lifecycle", "service", "lifecycle_state"),
        Index("ix_job_states_correlation_key", "correlation_key"),
    )

    # schema_version is a const "1" in the contract; stored so a future
    # breaking bump is detectable per-row.
    schema_version: Mapped[str] = mapped_column(
        String(8), nullable=False, default="1", server_default="1",
    )
    # PK = the service-assigned job id (AIFactory task_id "project:spec").
    job_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Upstream GitHub issue number (RFC-0001 correlation). Null until known.
    correlation_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
    )
    service: Mapped[str] = mapped_column(
        String(16), nullable=False, default="aifactory",
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="build",
    )
    # Canonical lifecycle: queued | running | review | done | failed | stuck.
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False)
    service_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default="1",
    )
    # admission{enqueued_at, queue_position, started_at} per the schema.
    admission: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # worker_ref{kind="subprocess"|"k8s-job", ...}. Phase 1 = subprocess.
    worker_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Persisted args needed to (re)spawn a queued/running build on drain or
    # restart-recovery. Small JSON (paths + flags), never a blob.
    spawn_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<JobState job_id={self.job_id!r} "
            f"state={self.lifecycle_state!r} attempt={self.attempt}>"
        )
