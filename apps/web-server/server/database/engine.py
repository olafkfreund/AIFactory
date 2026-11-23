"""
Async SQLAlchemy engine and session management.

Default backend is SQLite (aiosqlite) for local dev; production uses
Postgres (asyncpg) via the ``DATABASE_URL`` env var. The engine picks
the driver from the URL scheme, and the SQLite-specific WAL pragma
listener is only registered when the SQLite dialect is in use.
"""

import asyncio
import importlib
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from server.db_pool import pool_kwargs

from ..paths import get_data_dir

logger = logging.getLogger(__name__)

# SQLite default location: ~/.aifactory/data.db. These constants are kept
# at module level for backwards compatibility with init_db() and any
# external code that imports DATABASE_PATH for diagnostics.
DATABASE_DIR = get_data_dir()
DATABASE_PATH = DATABASE_DIR / "data.db"
_DEFAULT_SQLITE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"


def _resolve_database_url() -> str:
    """Resolve the active DATABASE_URL.

    Priority:
      1. ``DATABASE_URL`` env var — production sets this to
         ``postgresql+asyncpg://...``.
      2. SQLite fallback at ``~/.aifactory/data.db`` for local dev.

    Empty / whitespace-only env values are treated as unset.
    """
    raw = os.environ.get("DATABASE_URL", "").strip()
    return raw or _DEFAULT_SQLITE_URL


def _connect_args_for(url: str) -> dict:
    """Return driver-specific connect args.

    ``check_same_thread=False`` is a SQLite-only knob that the asyncpg
    driver rejects. Postgres needs no extra connect args here.
    """
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


DATABASE_URL = _resolve_database_url()

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args_for(DATABASE_URL),
    **pool_kwargs(DATABASE_URL, "DB", 2, 2),
)

logger.info(
    "Database engine bound: dialect=%s driver=%s",
    engine.dialect.name,
    engine.dialect.driver,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def _enable_wal_mode(dbapi_connection, connection_record):
    """Enable WAL mode on every new SQLite connection.

    WAL (Write-Ahead Logging) mode allows concurrent readers and a
    single writer, which is essential for a web server handling
    multiple simultaneous requests.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


# WAL is a SQLite-only feature. Register the listener only when the engine's
# dialect is SQLite — Postgres has its own concurrency model and doesn't
# need (or accept) the journal-mode PRAGMA.
if engine.dialect.name == "sqlite":
    event.listen(engine.sync_engine, "connect", _enable_wal_mode)


_ALEMBIC_INI_PATH = Path(__file__).resolve().parents[2] / "alembic.ini"
# Serialize boot migrations across AIFactory instances sharing a Postgres database.
_MIGRATION_LOCK_KEY = 0x4149464143545259


def _alembic_config():
    """Build an Alembic Config pointing at our alembic.ini + current DATABASE_URL.

    Imported lazily so alembic isn't a hard dependency for any code path
    that doesn't touch init_db(). We also rewrite ``script_location`` to an
    absolute path because Alembic resolves it relative to cwd, and init_db()
    can be invoked from any cwd (FastAPI app uses different working dirs
    than CLI / tests).
    """
    from alembic.config import Config

    cfg = Config(str(_ALEMBIC_INI_PATH))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    cfg.set_main_option(
        "script_location",
        str(_ALEMBIC_INI_PATH.parent / "server" / "database" / "alembic"),
    )
    return cfg


def _alembic_upgrade_head_sync() -> None:
    """Run `alembic upgrade head` programmatically. Synchronous (DDL transaction)."""
    from alembic import command

    command.upgrade(_alembic_config(), "head")


async def _upgrade_head_with_pg_lock() -> None:
    """Run the upgrade while holding a Postgres session advisory lock (#1998).

    The lock lives on its own NullPool engine so its connection is never
    handed back to the app pool, which may be used from another event loop.
    """
    lock_engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    try:
        async with lock_engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            # A pod that can't get the lock in 5 min fails boot loudly and restarts.
            await conn.execute(text("SET lock_timeout = '300s'"))
            await conn.execute(
                text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY}
            )
            try:
                await asyncio.to_thread(_alembic_upgrade_head_sync)
            finally:
                try:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(:k)"),
                        {"k": _MIGRATION_LOCK_KEY},
                    )
                except (SQLAlchemyError, OSError):
                    # Never mask the upgrade's error; closing the session frees the lock.
                    logger.warning(
                        "Failed to release migration advisory lock", exc_info=True
                    )
    finally:
        await lock_engine.dispose()


def _verify_schema_at_head_sync(sync_conn) -> None:
    """Raise RuntimeError if the DB schema isn't at the current head revision.

    Called when MIGRATIONS_AUTO_APPLY=false (out-of-band Helm Job mode).
    """
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    head_rev = script.get_current_head()

    ctx = MigrationContext.configure(sync_conn)
    current_rev = ctx.get_current_revision()

    if current_rev != head_rev:
        raise RuntimeError(
            f"Database schema is at {current_rev!r}, expected head {head_rev!r}. "
            "Run `alembic upgrade head` out-of-band (Helm Job), or set "
            "MIGRATIONS_AUTO_APPLY=true to apply on boot."
        )


async def init_db() -> None:
    """Initialize the database.

    Schema lifecycle depends on the ``MIGRATIONS_AUTO_APPLY`` setting:
      - true  → run `alembic upgrade head` (default; suits local dev)
      - false → verify schema is at head and fail fast otherwise
                (suits K8s deployments where a Helm Job runs Alembic
                before the app pods start, so the app role can lack
                DDL privileges)

    Then seeds the default user when ``DISABLE_AUTH`` is on.
    Safe to call multiple times — Alembic upgrades are idempotent.
    """
    # Lazy import to avoid a circular dependency between engine.py and config.py
    # at module load (config imports settings which imports paths which...).
    from ..config import get_settings

    settings = get_settings()

    # SQLite default needs its parent directory created; Postgres URLs don't.
    if engine.dialect.name == "sqlite":
        DATABASE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initializing SQLite database at {DATABASE_PATH}")
    else:
        logger.info(
            "Initializing database (dialect=%s, driver=%s)",
            engine.dialect.name,
            engine.dialect.driver,
        )

    if settings.MIGRATIONS_AUTO_APPLY:
        logger.info("MIGRATIONS_AUTO_APPLY=true — running `alembic upgrade head`")
        # Alembic uses a sync engine internally; run via to_thread so we
        # don't block the asyncio loop on the DDL transactions.
        if engine.dialect.name == "postgresql":
            await _upgrade_head_with_pg_lock()
        else:
            await asyncio.to_thread(_alembic_upgrade_head_sync)
    else:
        logger.info(
            "MIGRATIONS_AUTO_APPLY=false — verifying schema is at head "
            "(expecting an out-of-band Helm Job to have run migrations)"
        )
        async with engine.connect() as conn:
            await conn.run_sync(_verify_schema_at_head_sync)

    # SQLite-specific journal-mode log (Postgres has no PRAGMA equivalent).
    if engine.dialect.name == "sqlite":
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA journal_mode"))
            mode = result.scalar()
            logger.info(f"SQLite journal mode: {mode}")

    # Seed tenant defaults: the service/default user, the deployment "default"
    # org, memberships, and backfill of legacy projects (epic #318 / #319).
    # Best-effort — never blocks startup.
    await seed_tenant_defaults(settings.DISABLE_AUTH)

    logger.info("Database initialization complete")


# Stable ids for the deployment-wide service identity + default org (#319).
DEFAULT_USER_ID = "default"

# Re-exported, not defined here: ``project_registry`` needs it and must stay
# below the database layer (``_backfill_project_orgs`` below imports the
# registry). Every existing ``from ..database.engine import DEFAULT_ORG_ID``
# importer is unaffected.
from ..tenancy import DEFAULT_ORG_ID  # noqa: E402 - deliberate mid-file re-export


async def seed_tenant_defaults(disable_auth: bool) -> None:
    """Ensure the default service user, default org, memberships, and legacy
    project ownership exist (epic #318 / #319).

    Idempotent and best-effort: any failure is logged, never raised, so a
    seeding hiccup cannot block server startup.

    The default user is created **inactive** unless ``DISABLE_AUTH`` is on — so
    it satisfies the org-owner FK and the ``is_service`` authz bypass works,
    while ``get_current_user`` keeps rejecting it exactly as before (no
    expansion of the legacy ``API_TOKEN``'s reach to user-scoped routes).
    """
    try:
        from sqlalchemy import select

        from server.database.models import (  # noqa: PLC0415
            Organization,
            OrgMember,
            Team,
            TeamMembership,
            User,
        )

        async with async_session_factory() as session:
            # 1. Service/default user (FK target for the default org).
            user = await session.get(User, DEFAULT_USER_ID)
            if user is None:
                session.add(
                    User(
                        id=DEFAULT_USER_ID,
                        email="default@localhost",
                        name="Default User",
                        password_hash="disabled",
                        role="admin" if disable_auth else "user",
                        is_active=disable_auth,
                    )
                )
                await session.flush()
            elif disable_auth and not user.is_active:
                # DISABLE_AUTH turned on after the user was seeded inactive.
                user.is_active = True
                user.role = "admin"

            # 2. Deployment "default" organization. Idempotent on BOTH the id
            # AND the UNIQUE slug: if a "default"-slug org already exists under
            # a different id (legacy DB, an older seed, or the first-login
            # flow), a blind insert keyed only on id raises
            # "UNIQUE constraint failed: organizations.slug" on every startup.
            org = await session.get(Organization, DEFAULT_ORG_ID)
            if org is None:
                org = (
                    (
                        await session.execute(
                            select(Organization).where(Organization.slug == "default")
                        )
                    )
                    .scalars()
                    .first()
                )
            if org is None:
                org = Organization(
                    id=DEFAULT_ORG_ID,
                    name="Default",
                    slug="default",
                    owner_id=DEFAULT_USER_ID,
                    plan="free",
                )
                session.add(org)
                await session.flush()
            # Use the resolved org's actual id for the membership backfill — it
            # may differ from DEFAULT_ORG_ID in a legacy DB where the default
            # org was created under another id.
            default_org_id = org.id

            # 3. Make every existing user a member of the default org so legacy
            #    projects (backfilled below) stay accessible to current users.
            existing_members = set(
                (
                    await session.execute(
                        select(OrgMember.user_id).where(
                            OrgMember.org_id == default_org_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            all_users = (await session.execute(select(User.id, User.oidc_sub))).all()
            all_user_ids = [uid for uid, _ in all_users]
            oidc_user_ids = {
                uid for uid, sub in all_users if sub is not None and uid != DEFAULT_USER_ID
            }
            for uid in all_user_ids:
                if uid not in existing_members:
                    session.add(
                        OrgMember(
                            org_id=default_org_id,
                            user_id=uid,
                            role="owner" if uid == DEFAULT_USER_ID else "member",
                        )
                    )

            await session.flush()

            # Preserve existing org grants on each org's default team.
            org_members = (await session.execute(select(OrgMember))).scalars().all()
            teams = (await session.execute(select(Team))).scalars().all()
            default_teams = {team.org_id: team for team in teams if team.is_default}
            org_ids = (await session.execute(select(Organization.id))).scalars().all()
            for org_id in org_ids:
                if org_id not in default_teams:
                    team = next(
                        (
                            team
                            for team in teams
                            if team.org_id == org_id and team.slug == "default"
                        ),
                        None,
                    ) or Team(
                        id="team-default" if org_id == default_org_id else None,
                        org_id=org_id,
                        name="Default",
                        slug="default",
                        is_default=True,
                    )
                    team.is_default = True
                    session.add(team)
                    await session.flush()
                    default_teams[org_id] = team
            existing_team_members = set(
                (
                    await session.execute(
                        select(TeamMembership.team_id, TeamMembership.user_id)
                    )
                ).all()
            )
            for member in org_members:
                team_id = default_teams[member.org_id].id
                if (team_id, member.user_id) not in existing_team_members:
                    session.add(
                        TeamMembership(
                            team_id=team_id,
                            user_id=member.user_id,
                            role="owner"
                            if member.user_id == DEFAULT_USER_ID
                            else member.role,
                            provenance=(
                                "backfill" if member.user_id in oidc_user_ids else "manual"
                            ),
                        )
                    )

            await session.commit()

        # One-off id-preserving import of a legacy projects.json (#2157), before
        # serving; orgs exist by now so the project FK holds.
        from server.project_store import aimport_pending  # noqa: PLC0415

        await aimport_pending()
        _backfill_project_orgs()
    except Exception:
        logger.exception("seed_tenant_defaults failed (non-fatal)")


def _backfill_project_orgs() -> None:
    """Assign ``org_id=default`` to any registered project that lacks one."""
    try:
        # By name, not `import`: project_registry imports project_store, which
        # imports this module, and a static import closes that cycle.
        registry = importlib.import_module("server.project_registry")
    except Exception:
        return
    load_projects, save_projects = registry.load_projects, registry.save_projects
    projects = load_projects()
    changed = 0
    for proj in projects.values():
        if isinstance(proj, dict) and not proj.get("org_id"):
            proj["org_id"] = DEFAULT_ORG_ID
            changed += 1
    if changed:
        save_projects(projects)
        logger.info("Backfilled org_id=default on %d legacy project(s)", changed)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session.

    Usage in route handlers::

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Item))
            return result.scalars().all()

    The session is automatically closed when the request finishes.
    Commits must be done explicitly within the route handler.
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
