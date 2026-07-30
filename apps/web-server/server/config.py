"""
Configuration settings for AIFactory Web Server.

Settings are loaded from environment variables with sensible defaults.
"""

import logging
import secrets
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import get_data_dir, get_data_file

logger = logging.getLogger(__name__)

# Hosts that only accept local connections — DISABLE_AUTH is tolerable here.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def host_is_loopback(host: str) -> bool:
    """True when ``host`` only binds the loopback interface (not 0.0.0.0/::)."""
    return (host or "").strip().lower() in _LOOPBACK_HOSTS


def warn_if_auth_disabled(settings: "Settings") -> None:
    """Loud, unconditional warning whenever auth is bypassed (#324 H7)."""
    if settings.DISABLE_AUTH:
        logger.warning(
            "⚠️  APP_DISABLE_AUTH=true — authentication is BYPASSED; every "
            "request is treated as admin. Local development only; never expose "
            "this to a network."
        )


def enforce_disable_auth_safety(settings: "Settings") -> None:
    """Refuse to bind an unauthenticated admin API to a non-loopback host
    unless DEBUG explicitly accepts the risk (#324 H7).

    Called only from the real server entrypoint (``main.__main__``) — TestClient
    never reaches it, so the test suite (which runs with APP_DISABLE_AUTH=true
    and HOST=0.0.0.0) is unaffected.
    """
    if not settings.DISABLE_AUTH:
        return
    warn_if_auth_disabled(settings)
    if not host_is_loopback(settings.HOST) and not settings.DEBUG:
        raise SystemExit(
            "Refusing to start: APP_DISABLE_AUTH=true on non-loopback host "
            f"'{settings.HOST}' without APP_DEBUG. This would expose an "
            "unauthenticated admin API to the network. Bind APP_HOST to "
            "127.0.0.1, set APP_DEBUG=true to explicitly accept the risk for "
            "local development, or unset APP_DISABLE_AUTH."
        )


def cors_allows_credentials(settings: "Settings") -> bool:
    """Whether the CORS layer may safely echo credentials (#555).

    A wildcard origin (``*``) combined with ``allow_credentials=True`` lets any
    site read authenticated responses cross-origin — the browser only rejects
    the literal ``Access-Control-Allow-Origin: *`` + credentials pairing, not a
    reflected-origin variant some stacks emit. We never want that combination,
    so when ``CORS_ORIGINS`` contains a wildcard we drop credentials rather than
    fail open. Returns ``False`` to signal the caller must set
    ``allow_credentials=False``.
    """
    return "*" not in (settings.CORS_ORIGINS or [])


def enforce_cors_safety(settings: "Settings") -> None:
    """Refuse to bind a wildcard CORS origin alongside credentials (#555).

    Mirrors ``enforce_disable_auth_safety``: called only from the real server
    entrypoint. A wildcard origin with ``allow_credentials=True`` is a
    cross-origin credential-theft vector. In production (non-DEBUG) we refuse to
    boot so the misconfiguration is caught loudly; under ``APP_DEBUG`` we warn
    and let the app run with credentials dropped (see ``cors_allows_credentials``
    / the CORS middleware wiring in ``main.create_app``).
    """
    if cors_allows_credentials(settings):
        return
    logger.warning(
        "⚠️  CORS_ORIGINS contains '*' — credentialed CORS will be DISABLED "
        "(allow_credentials=False) to avoid a cross-origin credential-theft "
        "vector. Set APP_CORS_ORIGINS to an explicit allowlist."
    )
    if not settings.DEBUG:
        raise SystemExit(
            "Refusing to start: APP_CORS_ORIGINS contains a wildcard '*' "
            "origin. Combined with credentialed CORS this exposes "
            "authenticated responses to any site. Set APP_CORS_ORIGINS to an "
            "explicit allowlist, or set APP_DEBUG=true to run locally with "
            "credentialed CORS disabled."
        )


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Server configuration
    HOST: str = "0.0.0.0"
    PORT: int = 3101
    DEBUG: bool = False

    # SSL configuration
    SSL_ENABLED: bool = False
    SSL_CERTFILE: str = ""  # Path to SSL certificate
    SSL_KEYFILE: str = ""  # Path to SSL private key

    # Authentication
    API_TOKEN: str = Field(default="", repr=False)  # Will generate default if not set
    DISABLE_AUTH: bool = False  # Set to True to disable auth (dev only)

    # Scoped service tokens (Factory#312, step 1 — additive, off by default).
    # When true, a scoped ``acw_`` service key (no owning user) is recognised as
    # a first-class M2M principal that carries its EXPLICIT scopes on
    # ``request.state.user`` (``scopes`` + ``scoped_service`` markers), the seam
    # for replacing the wildcard ``API_TOKEN`` blanket bypass with per-scope
    # checks. Default false preserves today's behaviour exactly: the wildcard and
    # acw_ paths are byte-identical to pre-flag. Enforcement of those scopes lands
    # in a later phase (see docs/compliance/scoped-service-tokens.md); this flag
    # only makes the scopes visible. Override: APP_SCOPED_SERVICE_TOKENS_ENABLED.
    SCOPED_SERVICE_TOKENS_ENABLED: bool = False

    # JWT Configuration
    JWT_SECRET: str = Field(default="", repr=False)  # Auto-generated if not set
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ALGORITHM: str = "HS256"

    # Redis pub/sub for cross-replica WebSocket fan-out (Epic #35 #40).
    # Empty = in-process broadcasts only (laptop / single-replica K8s).
    # Multi-replica deployments set this from a Secret to enable Redis
    # bridging. ``REDIS_CHANNEL`` is overridable for shared Redis
    # instances where channel collision matters.
    REDIS_URL: str = ""
    REDIS_CHANNEL: str = "aifactory:events"

    # S3-compatible workspace storage (Epic #35 #40 half-B).
    # Empty = local-only (no snapshotting). Setting a non-empty fsspec
    # URI (s3://, gs://, azure://) enables snapshot-at-task-phase-
    # transition upload and lazy on-demand restore. Default preserves
    # v1.0 PVC-only behavior exactly. See docs/plans/2026-05-28-s3-workspaces-design.md.
    WORKSPACE_S3_URI_BASE: str = ""

    # Federated search (#149). The cockpit (CFactory) aggregates every portal's
    # work and exposes a ranked /api/search; this portal proxies to it so its ⌘K
    # palette offers the same cross-portal search same-origin. CFACTORY_SEARCH_URL
    # is the cockpit's in-cluster base; CFACTORY_READ_KEY is a read-scoped cockpit
    # key. Both empty = feature off (proxy returns an empty result set).
    CFACTORY_SEARCH_URL: str = "http://cfactory.factory.svc.cluster.local:3111"
    CFACTORY_READ_KEY: str = ""

    # Database
    DATABASE_URL: str = ""  # Auto-generated if not set (sqlite+aiosqlite:///...)
    # Alembic migration behaviour at app boot. P1.4 of Epic #26.
    #   true  → app boot runs `alembic upgrade head` (default; suits local
    #           dev + simple deployments)
    #   false → app boot only verifies the schema is at head and fails fast
    #           if not. Use this in K8s deployments where a Helm Job runs
    #           migrations out-of-band before the app pods start (allows
    #           the app role to lack DDL privileges).
    MIGRATIONS_AUTO_APPLY: bool = True

    # Paths
    PROJECTS_DATA_DIR: str = ""  # Directory to store project metadata
    BACKEND_PATH: str = ""  # Path to apps/backend

    # CORS — localhost defaults. Override or extend via APP_CORS_ORIGINS env var.
    # Accepts a comma-separated string ("https://a.com,https://b.com") or a JSON list.
    CORS_ORIGINS: list[str] = [
        "http://localhost:3100",
        "http://localhost:3000",
        "https://localhost:3100",
        "https://localhost:3000",
        "https://localhost:3101",
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                # Let pydantic handle JSON-list form natively
                return stripped
            return [s.strip() for s in stripped.split(",") if s.strip()]
        return v

    # Content-Security-Policy served with every HTML/document response.
    # `self`-only script sources are the point: the portal must never execute
    # third-party JavaScript (Monaco is self-hosted under /monaco/vs for exactly
    # this reason). `unsafe-eval` is required by Monaco's AMD loader and its
    # language workers; `unsafe-inline` styles by the bundled CSS-in-JS.
    # Override wholesale via APP_CONTENT_SECURITY_POLICY, or set it empty to
    # disable the header (e.g. when a reverse proxy sets its own).
    CONTENT_SECURITY_POLICY: str = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )

    # Terminal
    DEFAULT_SHELL: str = "/bin/bash"
    MAX_TERMINALS: int = 20

    # Task execution. Global cap on concurrently RUNNING builds, enforced by
    # AgentService admission control (RFC-0016 #668): at the cap, new builds are
    # queued FIFO instead of oversubscribing the pod. Env override
    # APP_MAX_CONCURRENT_TASKS; a value <= 0 means unlimited (pre-cap behaviour).
    MAX_CONCURRENT_TASKS: int = 5

    # Subprocess monitor tunables (#651) — surfaced on the pydantic-settings
    # boundary so the agent monitor loop has no bare magic constants. Defaults
    # preserve the previously hardcoded behaviour: poll/sync every 3s while a
    # task runs, and auto-continue a finished-but-incomplete build at most 10
    # times before giving up. Override with APP_AGENT_MONITOR_SYNC_INTERVAL /
    # APP_AGENT_MAX_CONTINUATION_ROUNDS.
    AGENT_MONITOR_SYNC_INTERVAL: float = 3.0
    AGENT_MAX_CONTINUATION_ROUNDS: int = 10

    # rmux Live Agent Console (Epic #44). Set APP_RMUX_ENABLED=true to surface
    # the live "Agent Console" tab. Honored by rmux/integration.is_enabled()
    # alongside the AIFACTORY_RMUX_ENABLED env var. Keep false on the
    # bank-pilot image (no bundled rmux binary).
    RMUX_ENABLED: bool = False

    # extra="ignore" (#384): the web-server's environment/.env legitimately
    # carries cross-cutting, non-APP_ vars meant for other components — notably
    # AIFACTORY_COMPLETION_WEBHOOK / _SENTINEL / _WEBHOOK_TIMEOUT, which
    # services/completion.py reads directly from os.environ to push RFC-0001
    # completion events to the CFactory cockpit. Without this, pydantic's default
    # extra="forbid" treats those keys as unknown fields and crashes startup.
    # Sibling services (PFactory/TFactory) already tolerate the extra.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="APP_",
        extra="ignore",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Generate default token if not set
        if not self.API_TOKEN:
            self.API_TOKEN = self._get_or_generate_token()

        # Generate JWT secret if not set
        if not self.JWT_SECRET:
            self.JWT_SECRET = self._get_or_generate_jwt_secret()

        # Set default paths
        if not self.BACKEND_PATH:
            # Assume we're in apps/web-server, backend is at ../backend
            self.BACKEND_PATH = str(Path(__file__).parent.parent.parent / "backend")

        if not self.PROJECTS_DATA_DIR:
            self.PROJECTS_DATA_DIR = str(get_data_dir())

        # Set default database URL
        if not self.DATABASE_URL:
            self.DATABASE_URL = f"sqlite+aiosqlite:///{self.PROJECTS_DATA_DIR}/data.db"

        # Set up SSL paths if enabled
        if self.SSL_ENABLED:
            self._setup_ssl()

    def _get_or_generate_token(self) -> str:
        """Get existing token or generate a new one."""
        token_file = get_data_file(".token")

        if token_file.exists():
            return token_file.read_text().strip()

        # Generate new token
        token = secrets.token_urlsafe(32)

        # Save token
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token)
        token_file.chmod(0o600)  # Owner read/write only

        # #324 (M1): never print the token value — stdout lands in container /
        # CI / journald logs. Point operators at the 0600 file instead.
        print(f"\n{'=' * 60}")
        print("AIFactory - First Run Setup")
        print(f"{'=' * 60}")
        print(f"Generated API token saved to: {token_file}")
        print("Read it with:")
        print(f"  cat {token_file}")
        print("Then authenticate API requests with:")
        print("  Authorization: Bearer <token>")
        print(f"{'=' * 60}\n")

        return token

    def _get_or_generate_jwt_secret(self) -> str:
        """Get existing JWT secret or generate a new one.

        The secret is persisted to ~/.aifactory/.jwt_secret so it
        survives server restarts, keeping existing tokens valid.
        """
        secret_file = get_data_file(".jwt_secret")

        if secret_file.exists():
            return secret_file.read_text().strip()

        # Generate new secret
        secret = secrets.token_urlsafe(32)

        # Save secret
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(secret)
        secret_file.chmod(0o600)  # Owner read/write only

        return secret

    def _setup_ssl(self) -> None:
        """Set up SSL certificates, generating self-signed if needed."""
        import subprocess

        ssl_dir = get_data_dir() / "ssl"
        ssl_dir.mkdir(parents=True, exist_ok=True)

        cert_file = ssl_dir / "cert.pem"
        key_file = ssl_dir / "key.pem"

        # Use provided paths or defaults
        if self.SSL_CERTFILE and self.SSL_KEYFILE:
            # User provided custom paths
            if not Path(self.SSL_CERTFILE).exists():
                raise ValueError(f"SSL certificate not found: {self.SSL_CERTFILE}")
            if not Path(self.SSL_KEYFILE).exists():
                raise ValueError(f"SSL key not found: {self.SSL_KEYFILE}")
            return

        # Generate self-signed certificate if not exists
        if not cert_file.exists() or not key_file.exists():
            print(f"\n{'=' * 60}")
            print("AIFactory - SSL Setup")
            print(f"{'=' * 60}")
            print("Generating self-signed SSL certificate...")

            try:
                subprocess.run(
                    [
                        "openssl",
                        "req",
                        "-x509",
                        "-newkey",
                        "rsa:4096",
                        "-keyout",
                        str(key_file),
                        "-out",
                        str(cert_file),
                        "-days",
                        "365",
                        "-nodes",
                        "-subj",
                        "/CN=localhost/O=AIFactory/C=US",
                    ],
                    check=True,
                    capture_output=True,
                )
                key_file.chmod(0o600)
                print(f"Certificate generated: {cert_file}")
                print(f"Private key generated: {key_file}")
                print("\nNOTE: This is a self-signed certificate.")
                print("Your browser will show a security warning.")
                print(f"{'=' * 60}\n")
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"Failed to generate SSL certificate: {e.stderr.decode()}"
                )
            except FileNotFoundError:
                raise RuntimeError(
                    "OpenSSL not found. Install OpenSSL to enable HTTPS."
                )

        # Set paths to generated certificates
        self.SSL_CERTFILE = str(cert_file)
        self.SSL_KEYFILE = str(key_file)


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get the settings instance."""
    return settings
