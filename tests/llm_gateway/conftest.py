"""Pytest fixtures for LLM-gateway tests (Epic #35 #38 PR-2b).

In-process SQLite + Base.metadata.create_all per test. Mirrors the
pattern in tests/audit/conftest.py + tests/test_tenant_reconciler.py.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER_ROOT = REPO_ROOT / 'apps' / 'web-server'
BACKEND_ROOT = REPO_ROOT / 'apps' / 'backend'

# Order matters: the web-server import of llm_audit_hook resolves
# server.services.llm_pii_redactor via the apps/backend path.
for _root in (WEB_SERVER_ROOT, BACKEND_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))


@pytest.fixture
def fresh_db():
    """In-memory SQLite + Base.metadata.create_all per test."""
    from server.database.models import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    nonce = secrets.token_hex(8)
    engine = create_async_engine(
        f'sqlite+aiosqlite:///file:llm-gw-{nonce}'
        f'?mode=memory&cache=shared&uri=true',
    )

    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_init())
    finally:
        loop.close()

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, SessionLocal
