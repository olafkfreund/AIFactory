"""
Async SQLAlchemy engine and session management.

Default backend is SQLite (aiosqlite) for local dev; production uses
Postgres (asyncpg) via the ``DATABASE_URL`` env var. The engine picks
the driver from the URL scheme, and the SQLite-specific WAL pragma
listener is only registered when the SQLite dialect is in use.
"""

import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base
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


async def init_db() -> None:
    """Initialize the database by creating all tables.

    Creates all tables defined in the ORM models. Safe to call multiple
    times -- existing tables are not recreated.
    """
    # SQLite default needs its parent directory; Postgres URLs don't.
    if engine.dialect.name == "sqlite":
        DATABASE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initializing SQLite database at {DATABASE_PATH}")
    else:
        logger.info(
            "Initializing database (dialect=%s, driver=%s)",
            engine.dialect.name, engine.dialect.driver,
        )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # SQLite-specific journal-mode check (Postgres has no PRAGMA equivalent).
    if engine.dialect.name == "sqlite":
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA journal_mode"))
            mode = result.scalar()
            logger.info(f"SQLite journal mode: {mode}")

    # Ensure a default user exists when auth is disabled
    from ..config import get_settings
    settings = get_settings()
    if settings.DISABLE_AUTH:
        from .models import User
        async with async_session_factory() as session:
            from sqlalchemy import select
            existing = await session.execute(
                select(User).where(User.id == "default")
            )
            if not existing.scalar_one_or_none():
                session.add(User(
                    id="default",
                    email="default@localhost",
                    name="Default User",
                    password_hash="disabled",
                    role="admin",
                    is_active=True,
                ))
                await session.commit()
                logger.info("Created default user for auth-disabled mode")

    logger.info("Database initialization complete")


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
