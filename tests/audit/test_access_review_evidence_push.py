"""Tests for the access-review evidence push (Factory#324).

Covers the codeable half of #324's "live evidence" AC: the control plane
must push its access-review export to the reserved MinIO drop-path
``factory-evidence/control-plane-push/access-review/<YYYY-MM-DD>.ndjson``.

- The fleet export produces one NDJSON line per member across all orgs,
  each carrying ``org_id`` + the shared access-review columns.
- The upload targets the correct dated evidence key.
- A push failure is swallowed (best-effort evidence, never crashes a caller).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

pytestmark = pytest.mark.audit


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_two_orgs(session_local: Any) -> None:
    from server.database.models import Organization, OrgMember, User

    async with session_local() as db:
        db.add_all(
            [
                User(id="u-a", email="a@c.com", password_hash="x", is_active=True),
                User(id="u-b", email="b@c.com", password_hash="x", is_active=False),
                Organization(id="org-1", name="Acme", slug="acme", owner_id="u-a"),
                Organization(id="org-2", name="Beta", slug="beta", owner_id="u-b"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                OrgMember(org_id="org-1", user_id="u-a", role="owner"),
                OrgMember(org_id="org-2", user_id="u-b", role="admin"),
            ]
        )
        await db.commit()


def test_push_targets_dated_evidence_key(fresh_db: Any) -> None:
    """The upload lands at control-plane-push/access-review/<day>.ndjson and
    the body is the fleet-wide NDJSON export."""
    from server.jobs.access_review_evidence_cron import push_access_review_evidence

    _engine, session_local = fresh_db
    captured: dict[str, bytes] = {}

    def _fake_upload(key: str, data: bytes) -> None:
        captured[key] = data

    async def _go() -> str | None:
        await _seed_two_orgs(session_local)
        async with session_local() as db:
            return await push_access_review_evidence(
                db, today=date(2026, 7, 25), upload=_fake_upload
            )

    uri = _run(_go())

    key = "control-plane-push/access-review/2026-07-25.ndjson"
    assert uri == f"s3://factory-evidence/{key}"
    assert key in captured

    lines = [json.loads(x) for x in captured[key].decode().splitlines()]
    assert len(lines) == 2
    by_org = {line["org_id"]: line for line in lines}
    assert set(by_org) == {"org-1", "org-2"}
    assert by_org["org-1"]["email"] == "a@c.com"
    assert by_org["org-1"]["role"] == "owner"
    assert by_org["org-2"]["active"] is False


def test_push_is_failsafe_on_upload_error(fresh_db: Any) -> None:
    """An upload exception is swallowed: returns None, never raises."""
    from server.jobs.access_review_evidence_cron import push_access_review_evidence

    _engine, session_local = fresh_db

    def _boom(key: str, data: bytes) -> None:
        raise RuntimeError("minio down")

    async def _go() -> str | None:
        await _seed_two_orgs(session_local)
        async with session_local() as db:
            return await push_access_review_evidence(
                db, today=date(2026, 7, 25), upload=_boom
            )

    assert _run(_go()) is None


def test_evidence_key_shape() -> None:
    from server.jobs.access_review_evidence_cron import evidence_key

    assert (
        evidence_key(date(2026, 1, 2))
        == "control-plane-push/access-review/2026-01-02.ndjson"
    )
