"""
Magestic AI Web Server - FastAPI Application.

Main entry point for the web server that provides:
- REST API for project/task management
- WebSocket endpoints for real-time streaming
- Static file serving for the React SPA
"""

import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Receive, Scope, Send

from . import env_bootstrap  # noqa: F401  — loads .env into os.environ first
from .auth import TokenAuthMiddleware
from .config import (
    cors_allows_credentials,
    enforce_cors_safety,
    enforce_disable_auth_safety,
    get_settings,
    warn_if_auth_disabled,
)
from .crypto.kms import enforce_kms_safety
from .database.engine import init_db
from .logging_config import setup_logging
from .routes import (
    api_keys,
    audit,
    auth_routes,
    auto_fix,
    capabilities,
    cli_accounts as cli_accounts_routes,
    context,
    copilot_mcp,
    email,
    execution,
    files,
    from_issue,
    git,
    git_credentials,
    github,
    llm_endpoints as llm_endpoints_routes,
    login_discovery as login_discovery_routes,
    logs as logs_routes,
    mcp,
    notifications,
    organizations,
    projects,
    search,
    settings as settings_routes,
    skills,
    stale,
    tasks,
    terminal,
    well_known,
)
from .services.skills_service import init_skills_service
from .websockets import (
    events as events_ws,
    logs as logs_ws,
    progress as progress_ws,
    terminal as terminal_ws,
)

# v3.0.2 — logging is configured INSIDE create_app() (was at module
# level until v3.0.1). Module-level setup_logging() was an import-
# side-effect that clobbered pytest's caplog handler whenever this
# module was imported during a test session, breaking ~7 unrelated
# stdlib-logging tests. Moving the call inside the factory means
# importing this module is a pure operation.
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    settings = get_settings()

    # Startup
    logger.info("Starting AIFactory Web Server...")
    logger.info(f"Backend path: {settings.BACKEND_PATH}")
    logger.info(f"Projects data dir: {settings.PROJECTS_DATA_DIR}")
    warn_if_auth_disabled(settings)

    # Ensure data directory exists
    Path(settings.PROJECTS_DATA_DIR).mkdir(parents=True, exist_ok=True)

    # Auto-configure autoBuildPath if the backend directory exists
    # (enables project initialization without manual settings configuration)
    backend_path = Path(settings.BACKEND_PATH)
    if backend_path.exists():
        from .routes.settings import load_app_settings, save_app_settings

        app_settings = load_app_settings()
        if not app_settings.autoBuildPath:
            app_settings.autoBuildPath = str(backend_path)
            save_app_settings(app_settings)
            logger.info(f"Auto-configured autoBuildPath: {backend_path}")

    # Initialize database (creates tables if needed)
    await init_db()

    # RFC-0016 #668: reconstruct durable admission state (cap/queue) from
    # Postgres so a restarted/new replica doesn't start from an empty
    # in-memory view. No-op when DATABASE_URL is unset (in-memory fallback).
    try:
        from .services.agent_service import get_agent_service

        await get_agent_service().reconcile_on_startup()
    except Exception:
        logger.warning("RFC-0016 startup reconcile failed (non-fatal)", exc_info=True)

    # Initialize skills service singleton once at startup
    init_skills_service()
    logger.info("SkillsService initialized")

    # OpenTelemetry distributed tracing (Epic #35 #42 PR-1). Idempotent;
    # no-op exporter when OTEL_EXPORTER_OTLP_ENDPOINT is unset — spans
    # still build in memory at near-zero cost. The middleware order
    # below depends on tracing running BEFORE CorrelationIdMiddleware
    # so the FastAPI auto-instrumentor opens the root span first.
    from .observability.tracing import init_tracing

    init_tracing()

    # Start the Redis pub/sub subscriber when REDIS_URL is configured
    # (Epic #35 #40 PR-1). No-op when unset — the event bus runs in
    # in-process-only mode and own-replica delivery still works.
    from .websockets import event_bus

    if settings.REDIS_URL:
        await event_bus.start_redis_subscriber()
        logger.info(
            "Redis pub/sub enabled — replica %s, channel %r",
            event_bus.self_replica_id,
            settings.REDIS_CHANNEL,
        )
    else:
        logger.info(
            "Redis pub/sub disabled (REDIS_URL unset) — in-process broadcasts only"
        )

    # Start the completion-event outbox relay when enabled (#465). Off by
    # default — the direct webhook path in completion.py is unchanged until the
    # operator opts in with AIFACTORY_COMPLETION_OUTBOX=true. The relay drains
    # durably-queued terminal events with retry/backoff (at-least-once delivery).
    import asyncio as _asyncio

    from .services import outbox as _outbox

    app.state.outbox_relay_stop = None
    app.state.outbox_relay_task = None
    if _outbox.outbox_enabled():
        stop = _asyncio.Event()
        app.state.outbox_relay_stop = stop
        app.state.outbox_relay_task = _asyncio.create_task(
            _outbox.relay_loop(stop=stop)
        )
        logger.info("Completion outbox relay enabled (at-least-once delivery)")
    else:
        logger.info(
            "Completion outbox relay disabled (AIFACTORY_COMPLETION_OUTBOX unset)"
        )

    # Start the RFC-0011 label-driven intake poller when enabled (#636). Off by
    # default — only runs when AIFACTORY_INTAKE_POLLER is set. Polls the
    # configured repos for factory:* labels and routes by difficulty tier with
    # two-guard (SQLite + factory:queued) exactly-once idempotency.
    from .services import intake_poller as _intake

    app.state.intake_poller_stop = None
    app.state.intake_poller_task = None
    if _intake.poller_enabled():
        intake_stop = _asyncio.Event()
        app.state.intake_poller_stop = intake_stop
        app.state.intake_poller_task = _asyncio.create_task(
            _intake.poller_loop(stop=intake_stop)
        )
        logger.info("RFC-0011 intake poller enabled")
    else:
        logger.info("RFC-0011 intake poller disabled (AIFACTORY_INTAKE_POLLER unset)")

    # RFC-0016 #671 control/execution split: when builds run as k8s Jobs
    # (AIFACTORY_BUILD_BACKEND=kubejob), the control plane reconciles by polling
    # Postgres + reaps vanished Jobs on an interval, so a missed completion event
    # never strands a build. Off by default — no-op for the in-pod subprocess
    # backend (the loop is only started when the kubejob backend is enabled).
    from .services.build_backend import kubejob_enabled as _kubejob_enabled

    app.state.kubejob_reconcile_stop = None
    app.state.kubejob_reconcile_task = None
    if _kubejob_enabled():
        kj_stop = _asyncio.Event()
        app.state.kubejob_reconcile_stop = kj_stop
        app.state.kubejob_reconcile_task = _asyncio.create_task(
            get_agent_service().kubejob_reconcile_loop(stop=kj_stop)
        )
        logger.info(
            "RFC-0016 #671 kubejob build backend enabled — reconcile loop started"
        )
    else:
        logger.info("RFC-0016 #671 kubejob build backend disabled (subprocess default)")

    # #1064: sweep tasks whose worker died and left them looking active. Runs
    # here rather than as a CronJob because the spec tree lives on a RWO
    # local-path PVC -- a separate pod scheduled off-node would find nothing,
    # report zero orphans and exit green. Off by default; report-only until
    # REAPER_DRY_RUN=false.
    from .services import stale_reaper as _reaper  # noqa: PLC0415

    app.state.stale_reaper_stop = None
    app.state.stale_reaper_task = None
    if _reaper.reaper_enabled():
        reaper_stop = _asyncio.Event()
        app.state.stale_reaper_stop = reaper_stop
        app.state.stale_reaper_task = _asyncio.create_task(
            _reaper.reaper_loop(stop=reaper_stop)
        )
        logger.info(
            "Stale-task reaper enabled (%s)",
            "report-only" if _reaper.dry_run() else "WRITING: orphans get cancelled",
        )
    else:
        logger.info("Stale-task reaper disabled (AIFACTORY_STALE_REAPER unset)")

    yield

    # Shutdown
    logger.info("Shutting down Magestic AI Web Server...")
    if settings.REDIS_URL:
        await event_bus.stop_redis_subscriber()
        # RFC-0017 #681: close the rmux Redis-transport client too (no-op off).
        try:
            from .rmux import redis_transport as _rmux_redis

            await _rmux_redis.close()
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.debug("rmux redis_transport close failed", exc_info=True)
    if app.state.outbox_relay_task is not None:
        app.state.outbox_relay_stop.set()
        try:
            await _asyncio.wait_for(app.state.outbox_relay_task, timeout=5.0)
        except (TimeoutError, _asyncio.CancelledError):
            app.state.outbox_relay_task.cancel()
    if app.state.stale_reaper_task is not None:
        app.state.stale_reaper_stop.set()
        try:
            await _asyncio.wait_for(app.state.stale_reaper_task, timeout=5.0)
        except (TimeoutError, _asyncio.CancelledError):
            app.state.stale_reaper_task.cancel()
    if app.state.intake_poller_task is not None:
        app.state.intake_poller_stop.set()
        try:
            await _asyncio.wait_for(app.state.intake_poller_task, timeout=5.0)
        except (TimeoutError, _asyncio.CancelledError):
            app.state.intake_poller_task.cancel()
    if app.state.kubejob_reconcile_task is not None:
        app.state.kubejob_reconcile_stop.set()
        try:
            await _asyncio.wait_for(app.state.kubejob_reconcile_task, timeout=5.0)
        except (TimeoutError, _asyncio.CancelledError):
            app.state.kubejob_reconcile_task.cancel()


def _read_app_version() -> str:
    """Return the canonical package version.

    Reads ``apps/backend/__init__.py``'s ``__version__`` — that's the
    file bump-version.js updates on every release. We deliberately
    don't ``from apps.backend import __version__`` because the
    web-server's PYTHONPATH doesn't reliably include the repo root in
    every install layout (especially the container image). Reading
    the file via a relative path keeps it robust.
    """
    import re
    from pathlib import Path

    backend_init = Path(__file__).resolve().parents[2] / "backend" / "__init__.py"
    # ponytail: version file missing/unreadable just falls back to unknown
    with suppress(OSError):
        content = backend_init.read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)
    return "0.0.0-unknown"


class SPAStaticFiles(StaticFiles):
    """StaticFiles for the React SPA mounted at the catch-all ``/``.

    Two behaviours on top of Starlette's StaticFiles:

    1. ``__call__`` guards non-HTTP scopes. The ``/`` mount is a catch-all, so a
       websocket connection to a path with no matching WS route falls through to
       here. Starlette's ``StaticFiles.__call__`` opens with
       ``assert scope["type"] == "http"``, which raised ``AssertionError`` for
       every stray websocket — spamming the logs and breaking the handshake with
       a 500 instead of a clean rejection. Registered WS routes (``/ws/*``,
       agent-console) are matched before this mount and are unaffected.
    2. ``get_response`` does SPA history-fallback (serve ``index.html`` for
       extension-less 404 GETs) and sets the SSO-safe cache policy.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1000})
            return
        await super().__call__(scope, receive, send)

    async def get_response(self, path, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # SPA history-fallback: React Router owns client-side routes
            # like /console/<pid>/<spec>. The static mount has no file at
            # those paths, so StaticFiles raises 404. Serve index.html
            # instead and let the SPA router resolve the path — otherwise
            # deep-links and hard refreshes return {"detail":"Not Found"}.
            #
            # Only fall back for genuine SPA navigations: a 404 on a GET
            # whose last path segment has no file extension (i.e. not a
            # missing /assets/foo.js). Anything with an extension is a real
            # missing asset and should keep its 404. API routes never reach
            # here — their routers are matched before this "/" mount.
            request_path = scope.get("path") or ""
            last_segment = request_path.rsplit("/", 1)[-1]
            if (
                exc.status_code == 404
                and scope.get("method", "GET") == "GET"
                and "." not in last_segment
            ):
                response = await super().get_response("index.html", scope)
            else:
                raise
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif "/assets/" in (scope.get("path") or ""):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    # v3.0.2 — stdlib logging configured here (was module-level
    # in v3.0.0/v3.0.1; see note at the top of this file).
    setup_logging(log_level="DEBUG" if settings.DEBUG else "INFO")

    # Epic #26 P6 (wired in v3.0.2) — structlog JSON-to-stdout logging.
    # Configured here so every log line emitted during create_app() +
    # lifespan startup is JSON-formatted from the very first event.
    # Idempotent: re-calling overrides the processor chain wholesale.
    from .observability import configure_structlog

    configure_structlog(level="DEBUG" if settings.DEBUG else "INFO")

    # Version comes from apps/backend/__init__.py — the canonical source
    # of truth that bump-version.js updates on every release. Reading it
    # at runtime avoids the v3.0.0/v3.0.1 drift where main.py's
    # hardcoded "1.0.0" lagged behind the actual package version.
    _app_version = _read_app_version()

    app = FastAPI(
        title="AIFactory Web API",
        description="Web API for AIFactory — self-hosted AI task management + agent orchestration",
        version=_app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    # OTel FastAPI instrumentation (Epic #35 #42 PR-1). Installs at
    # the ASGI layer so the request span is opened BEFORE any user
    # middleware (incl. CorrelationIdMiddleware below) runs. That
    # lets CorrelationIdMiddleware source request_id from trace_id.
    # No-op when OTel isn't installed (failure-safe per the helper).
    from .observability.tracing import instrument_fastapi_app

    instrument_fastapi_app(app)

    # Add CORS middleware. Never combine a wildcard origin with credentials
    # (#555): when CORS_ORIGINS contains '*', drop allow_credentials so we don't
    # expose authenticated responses cross-origin. enforce_cors_safety() (called
    # from the real entrypoint) refuses to boot on this misconfig in production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=cors_allows_credentials(settings),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add token auth middleware
    app.add_middleware(TokenAuthMiddleware)

    # Epic #26 P3 — SessionMiddleware powers authlib's PKCE-verifier +
    # state round-trip between /api/auth/oidc/login and /callback. The
    # secret is the JWT secret (already a strong process secret).
    # Cookie is scoped to the OIDC routes via SameSite=Lax + HTTP-only.
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.JWT_SECRET,
        session_cookie="aif_oidc_session",
        max_age=600,  # 10 min — enough to complete the OIDC redirect dance
        same_site="lax",
        https_only=False,  # operator's reverse-proxy adds Secure
    )

    # Epic #26 P6 (wired in v3.0.2) — CorrelationIdMiddleware. Added
    # LAST so it's the outermost layer: it sets the X-Request-ID
    # contextvar BEFORE TokenAuth runs (so 401-rejected requests still
    # carry the ID in their response, which auditors rely on to trace
    # failed auth attempts).
    from .observability import CorrelationIdMiddleware, install_httpx_propagation

    app.add_middleware(CorrelationIdMiddleware)
    # Patch httpx clients to forward the correlation ID on outbound
    # calls. Idempotent.
    install_httpx_propagation()

    # Security response headers. The Content-Security-Policy is the load-bearing
    # one: it pins script execution to our own origin, so a compromised CDN (or
    # any injected third-party <script>) cannot run inside an authenticated
    # portal session. Monaco used to be fetched from cdn.jsdelivr.net at runtime
    # and is now self-hosted under /monaco/vs precisely so this policy can hold.
    # Empty CONTENT_SECURITY_POLICY disables the header for operators whose
    # reverse proxy sets its own.
    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        csp = settings.CONTENT_SECURITY_POLICY
        if csp:
            response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # Auth routes (prefix defined in router: /api/auth)
    app.include_router(auth_routes.router)

    # Epic #26 P3 — OIDC SSO routes (prefix defined in router: /api/auth/oidc).
    # Endpoints return 404 when APP_OIDC_ENABLED isn't set, so this is a
    # no-op for installations that haven't configured an IdP.
    from .routes import oidc_routes

    app.include_router(oidc_routes.router)

    # Epic #35 #41 PR-1b4 — IdP discovery endpoint.  Always mounted (no
    # feature flag) because the login page must be able to call it even
    # before knowing which protocols are configured.  Returns [] when
    # nothing is enabled — the login page falls back to local-password form.
    app.include_router(login_discovery_routes.router)

    # Epic #35 #41 — SAML 2.0 SP + SCIM 2.0 routers, only mounted when
    # operator-enabled. Earlier draft of PR-1b4 mounted these unconditionally,
    # which dragged python3-saml + scim models into every test-suite app
    # construction via TestClient's lifespan, causing cross-test contamination
    # of the engine + SAML singletons. Gating on env vars keeps the default
    # deployment + test runs unaffected; production deployments that enable
    # SAML or SCIM via Helm get the routers mounted at pod start.
    import os

    if os.environ.get("SAML_ENABLED", "").lower() == "true":
        from .saml import routes as saml_routes

        app.include_router(saml_routes.router)
    if os.environ.get("SCIM_ENABLED", "").lower() == "true":
        from .scim import routes as scim_routes

        app.include_router(scim_routes.router)

    # Organization routes (prefix defined in router: /api/orgs)
    app.include_router(organizations.router)

    # API key management (prefix defined in router: /api/keys)
    app.include_router(api_keys.router)

    # Git credentials for portal-managed clone auth (epic #82 PR-C)
    app.include_router(git_credentials.router)

    # Audit log routes (prefix defined in router: /api/orgs)
    app.include_router(audit.router)

    # Epic #26 P5.3 — /api/audit/export streaming + P5.5 GDPR erasure.
    app.include_router(audit.export_router)
    from .routes import gdpr as gdpr_routes

    app.include_router(gdpr_routes.router)

    # Epic #35 #43 PR-1b4 — /api/admin/access-review endpoint for
    # SOC2 CC6.2 + ISO 27001 A.9.2.5 quarterly access reviews.
    from .routes import access_review as access_review_routes

    app.include_router(access_review_routes.router)

    # Notification routes (prefix defined in router: /api/notifications)
    app.include_router(notifications.router)

    # Include API routers
    app.include_router(projects.router, prefix="/api/projects", tags=["Projects"])
    # Execution routes also live under /api/tasks. They MUST be registered
    # before tasks.router, whose catch-all GET /{task_id} would otherwise
    # shadow specific paths like GET /api/tasks/running (returned 400 on /running).
    app.include_router(execution.router, prefix="/api/tasks", tags=["Task Execution"])
    # RFC-0011 label-driven intake: POST /api/tasks/from-issue. Registered
    # before tasks.router so its specific path isn't shadowed by the catch-all.
    app.include_router(from_issue.router, prefix="/api/tasks", tags=["Intake"])
    app.include_router(tasks.router, prefix="/api/tasks", tags=["Tasks"])
    app.include_router(
        settings_routes.router, prefix="/api/settings", tags=["Settings"]
    )
    app.include_router(
        cli_accounts_routes.router, prefix="/api/settings", tags=["CLI Accounts"]
    )
    app.include_router(llm_endpoints_routes.router)
    app.include_router(files.router, prefix="/api/files", tags=["Files"])
    app.include_router(terminal.router, prefix="/api/terminals", tags=["Terminals"])

    # Email OAuth + account management routes (prefix defined in router: /api/email)
    app.include_router(email.router)

    # GitHub routes
    app.include_router(github.router, prefix="/api/github", tags=["GitHub"])

    # Capability discovery (Epic #44 R2) — always mounted; the frontend
    # consults this on load to know whether to render the Live Agent
    # Console tab.  The router already declares its own prefix.
    app.include_router(capabilities.router, tags=["Capabilities"])

    # Agent-skills manifest (RFC-0019 §3.4) — always mounted, and readable
    # WITHOUT auth: an agent enumerates what this service can do before it
    # holds a token.  TokenAuthMiddleware only guards /api/*, so the
    # /.well-known/ path is exempt by construction.  Registered ahead of the
    # SPA catch-all "/" static mount so it resolves to JSON, not index.html.
    app.include_router(well_known.router)
    # Orphaned-task reaper: a task whose worker died stays in a
    # machine-owned state forever and shows as active in the cockpit.
    app.include_router(stale.router)

    app.include_router(search.router, tags=["Search"])
    app.include_router(mcp.router)
    # Copilot-facing MCP server at /mcp (JSON-RPC 2.0, POST-only).
    # Requires AIFACTORY_MCP_SECRET in env; accepts all requests in dev mode.
    app.include_router(copilot_mcp.router)

    # Remote HTTP+SSE MCP server (Epic #50 / Issue #83) — opt-in via
    # AIFACTORY_MCP_REMOTE_ENABLED=true.  Exposes the AIFactory task
    # control plane to non-Claude MCP clients (Cursor, Continue.dev,
    # custom scripts).
    from . import mcp_remote

    if mcp_remote.is_enabled():
        from .mcp_remote.server import router as mcp_remote_router

        app.include_router(mcp_remote_router)
        logger.info("Remote MCP server enabled — mounted at /api/mcp-remote")

    # Stdio MCP control-plane proxy (Issue #154).
    # acw_-keyed proxy in front of the operations the stdio MCP exercises,
    # so enterprise installs can hand each laptop a scoped key instead of
    # the host-wide admin token. Legacy admin token still works as a wildcard
    # so v1.0 single-user deployments are unaffected — always mounted.
    from .mcp_stdio import router as mcp_stdio_router

    app.include_router(mcp_stdio_router)
    logger.info("Stdio MCP proxy mounted at /api/mcp-stdio")

    # Auto-Fix routes (multi-provider polling backing useAutoFix.ts).
    # Endpoints live under /api/projects/{id}/auto-fix/* so they match
    # the per-project nesting the frontend already follows.
    app.include_router(auto_fix.router, prefix="/api/projects", tags=["Auto-Fix"])

    # Git and utility routes
    app.include_router(git.router, prefix="/api/git", tags=["Git"])
    app.include_router(git.ollama_router, prefix="/api/ollama", tags=["Ollama"])
    app.include_router(
        git.claude_code_router, prefix="/api/claude-code", tags=["Claude Code"]
    )
    app.include_router(git.mcp_router, prefix="/api/mcp", tags=["MCP"])
    app.include_router(git.updates_router, prefix="/api/updates", tags=["Updates"])

    # Memory infrastructure routes
    app.include_router(context.router, prefix="/api/memory", tags=["Memory"])

    # Logs viewing routes
    app.include_router(logs_routes.router, prefix="/api/logs", tags=["Logs"])

    # Skills knowledge base routes
    app.include_router(skills.router, prefix="/api/skills", tags=["Skills"])

    # Include WebSocket routers
    app.include_router(logs_ws.router, tags=["WebSocket"])
    app.include_router(progress_ws.router, tags=["WebSocket"])
    app.include_router(terminal_ws.router, tags=["WebSocket"])
    app.include_router(events_ws.router, tags=["WebSocket"])

    # Epic #44 R1 — rmux Live Agent Console (opt-in).  Only mount when
    # AIFACTORY_RMUX_ENABLED=true; otherwise the agent-console routes
    # are 404 and the JS frontend hides the Live Console tab.  Bank
    # pilot image (WITH_RMUX=false) cannot reach this code anyway —
    # the rmux/ package imports break at module load when the
    # bundled binary isn't present.
    from .rmux import is_rmux_enabled

    if is_rmux_enabled():
        from .rmux import console_router

        app.include_router(console_router)
        logger.info("[main] rmux Live Agent Console enabled — bridge router mounted")

    # Epic #26 P6 (wired in v3.0.2) — Prometheus /metrics. Called
    # AFTER all routers are mounted so the instrumentator can derive
    # cardinality-capped `handler` labels from FastAPI's route table.
    # Optional METRICS_SCRAPE_TOKEN bearer gate is read from env at
    # install time.
    from .observability import install_metrics

    install_metrics(app)

    # Health check endpoint (no auth required)
    @app.get("/api/health")
    async def health_check():
        from .provider_health import provider_credential_health

        return {
            "status": "healthy",
            "version": app.version,
            # Provider-credential config pre-flight (#109) — booleans only, no
            # secrets; CFactory surfaces it as a provider-auth health tile.
            "provider_auth": provider_credential_health(),
        }

    # Mount static files for SPA (if build directory exists).
    #
    # Cache policy (SSO fix) — the SPA shell (index.html) MUST NOT be
    # heuristically cached by the browser, or users keep running the previous
    # build's JS after an upgrade (which is exactly how a fixed auth bug can
    # look unfixed). Starlette's StaticFiles sends ETag/Last-Modified but no
    # Cache-Control, which triggers browser heuristic caching of the shell.
    # So: HTML responses → `no-cache` (store but always revalidate; cheap 304s);
    # content-hashed assets under /assets/ → long-lived immutable cache.
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount(
            "/", SPAStaticFiles(directory=str(static_dir), html=True), name="static"
        )
    else:
        # Placeholder for development
        @app.get("/")
        async def root():
            return {
                "message": "AIFactory Web Server",
                "docs": "/docs",
                "note": "Frontend not built yet. Run 'npm run build' in apps/frontend-web/",
            }

    return app


# Create the app instance
app = create_app()


# Add Bearer token auth to OpenAPI schema so Swagger UI shows the Authorize button
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT access token or legacy API token",
        }
    }
    openapi_schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = openapi_schema
    return openapi_schema


app.openapi = custom_openapi


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()

    # #324 (H7): never bind an unauthenticated admin API to the network.
    enforce_disable_auth_safety(settings)

    # #555: never bind a wildcard CORS origin alongside credentialed CORS.
    enforce_cors_safety(settings)

    # #1290: never come up pretending to encrypt. A selected KMS backend that
    # cannot be constructed would silently write credentials in PLAINTEXT.
    enforce_kms_safety()

    # Build uvicorn config
    uvicorn_config = {
        "app": "server.main:app",
        "host": settings.HOST,
        "port": settings.PORT,
        "reload": settings.DEBUG,
    }

    # Add SSL if enabled
    if settings.SSL_ENABLED:
        uvicorn_config["ssl_certfile"] = settings.SSL_CERTFILE
        uvicorn_config["ssl_keyfile"] = settings.SSL_KEYFILE
        logger.info(f"HTTPS enabled with certificate: {settings.SSL_CERTFILE}")

    uvicorn.run(**uvicorn_config)
