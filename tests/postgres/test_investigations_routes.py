"""Investigation routes through the app's auth middleware and Postgres store."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.postgres.helpers import run_alembic

WEB = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(WEB) not in sys.path:
    sys.path.append(str(WEB))

from server import auth, project_registry  # noqa: E402
from server.config import get_settings  # noqa: E402
from server.database.engine import get_db  # noqa: E402
from server.database.models import Organization, OrgMember, User  # noqa: E402
from server.main import create_app  # noqa: E402
from server.routes import investigations  # noqa: E402
from server.services.investigation_backend import InvestigationBackend  # noqa: E402
from server.services.investigation_store import InvestigationStore  # noqa: E402

from tests.postgres.test_investigation_backend import FakeKube  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.slow]


@pytest.fixture
def env(test_postgres_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    assert (
        run_alembic(
            ["upgrade", "head"], env={"DATABASE_URL": test_postgres_url}
        ).returncode
        == 0
    )
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "on")
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    monkeypatch.setenv("DATABASE_URL", test_postgres_url)

    settings = get_settings()
    monkeypatch.setattr(settings, "DISABLE_AUTH", False)
    monkeypatch.setattr(settings, "API_TOKEN", "test-token")
    engine = create_async_engine(test_postgres_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = InvestigationStore(sessions)
    org_id, project_id, run_id, ended_id = (str(uuid4()) for _ in range(4))
    users = {"viewer": str(uuid4()), "editor": str(uuid4())}
    password_hash = hashlib.sha256(org_id.encode()).hexdigest()
    monkeypatch.setattr(
        auth,
        "_try_decode_jwt",
        lambda token: {
            "sub": users[token],
            "role": "user",
            "email": f"{token}@example.test",
        }
        if token in users
        else None,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    tokens = {run_id: "run-token", ended_id: "ended-token"}

    async def seed() -> None:
        async with sessions() as session:
            session.add_all(
                [
                    User(
                        id=users["viewer"],
                        email=f"viewer-{org_id}@example.test",
                        password_hash=password_hash,
                    ),
                    User(
                        id=users["editor"],
                        email=f"editor-{org_id}@example.test",
                        password_hash=password_hash,
                    ),
                ]
            )
            await session.flush()
            session.add(
                Organization(
                    id=org_id, name="test", slug=org_id, owner_id=users["editor"]
                )
            )
            await session.flush()
            session.add_all(
                [
                    OrgMember(org_id=org_id, user_id=users["viewer"], role="viewer"),
                    OrgMember(org_id=org_id, user_id=users["editor"], role="member"),
                ]
            )
            await session.commit()
        for rid, gone in ((run_id, False), (ended_id, True)):
            await store.insert(
                id=rid,
                project_id=project_id,
                repo="test",
                sha="a" * 40,
                request="find issue",
                test_command="pytest",
                limits={"minutes": 30, "tokens": 1000, "turns": 5, "test_calls": 1},
                state="completed" if gone else "running",
                pods_gone=gone,
                lease_expires_at=now + timedelta(seconds=60),
                token_hash=hashlib.sha256(tokens[rid].encode()).hexdigest(),
            )
        await store.append_logs(run_id, ["one", "two", "three"])

    asyncio.run(seed())
    projects = {project_id: {"path": str(tmp_path), "org_id": org_id}}

    async def aload() -> dict[str, dict[str, str]]:
        return projects

    # #2157 moved project lookup to the async DB registry (aload_projects).
    monkeypatch.setattr(project_registry, "load_projects", lambda: projects)
    monkeypatch.setattr(project_registry, "aload_projects", aload)
    monkeypatch.setattr(investigations, "aload_projects", aload)

    async def db():
        async with sessions() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = db
    backend = InvestigationBackend(store)
    backend.renew_lease = AsyncMock()
    backend.stop = AsyncMock(return_value={"state": "stopped", "pods_gone": True})
    app.state.investigation_backend = backend
    client = TestClient(app)
    yield SimpleNamespace(
        client=client,
        backend=backend,
        run_id=run_id,
        ended_id=ended_id,
        project_id=project_id,
        tmp_path=tmp_path,
    )
    asyncio.run(engine.dispose())


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def test_drain_keeps_existing_routes_and_refuses_create(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "drain")
    app = create_app()
    app.dependency_overrides.update(env.client.app.dependency_overrides)
    app.state.investigation_backend = env.backend
    client = TestClient(app)
    base = f"/api/investigations/{env.run_id}"
    body = {"project_id": env.project_id, "sha": "a" * 40, "request": "find issue"}
    for path in ("/api/investigations", "/api/mcp-stdio/investigations"):
        response = client.post(
            path, json=body, headers=bearer("test-token" if "mcp" in path else "editor")
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "investigations are draining"

    assert client.get(base, headers=bearer("viewer")).status_code == 200
    assert client.get(f"{base}/logs", headers=bearer("viewer")).status_code == 200
    assert client.post(f"{base}/lease", headers=bearer("editor")).status_code == 200
    assert client.post(f"{base}/stop", headers=bearer("editor")).status_code == 200

    monkeypatch.setattr(env.backend, "append_log", AsyncMock())
    monkeypatch.setattr(env.backend, "run_tests", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(env.backend, "put_report", AsyncMock())
    monkeypatch.setattr(
        env.backend, "lease_status", AsyncMock(return_value={"ok": True})
    )
    headers = {"X-Run-Token": "run-token"}
    assert (
        client.post(
            f"{base}/pod/log", json={"lines": ["hello"]}, headers=headers
        ).status_code
        == 200
    )
    assert client.post(f"{base}/pod/run-tests", headers=headers).status_code == 200
    assert (
        client.post(
            f"{base}/pod/report", json={"findings": "ok"}, headers=headers
        ).status_code
        == 200
    )
    assert client.get(f"{base}/pod/lease", headers=headers).status_code == 200
    assert client.get(f"{base}/pod/config", headers=headers).status_code == 200


def test_pod_token_scope_and_caller_separation(env: SimpleNamespace) -> None:
    base = f"/api/investigations/{env.run_id}"
    assert env.client.get(f"{base}/pod/config").status_code == 401
    assert (
        env.client.get(
            f"{base}/pod/config", headers={"X-Run-Token": "ended-token"}
        ).status_code
        == 403
    )
    assert (
        env.client.get(
            f"/api/investigations/{env.ended_id}/pod/config",
            headers={"X-Run-Token": "ended-token"},
        ).status_code
        == 403
    )
    assert env.client.get(base, headers=bearer("run-token")).status_code == 401
    assert (
        env.client.post(
            f"{base}/lease", headers={"X-Run-Token": "run-token"}
        ).status_code
        == 401
    )
    assert env.client.get(
        f"{base}/pod/config", headers={"X-Run-Token": "run-token"}
    ).json() == {
        "request": "find issue",
        "sha": "a" * 40,
        "limits": {"minutes": 30, "tokens": 1000, "turns": 5, "test_calls": 1},
        "has_tests": True,
    }


def test_roles_cursor_and_stop(env: SimpleNamespace) -> None:
    base = f"/api/investigations/{env.run_id}"
    assert env.client.get(base, headers=bearer("viewer")).status_code == 200
    assert env.client.post(f"{base}/lease", headers=bearer("viewer")).status_code == 403
    assert env.client.post(f"{base}/stop", headers=bearer("viewer")).status_code == 403
    first = env.client.get(f"{base}/logs?limit=2", headers=bearer("viewer"))
    assert first.status_code == 200
    assert [e["line"] for e in first.json()["entries"]] == ["one", "two"]
    cursor = first.json()["cursor"]
    second = env.client.get(
        f"{base}/logs?after={cursor}&limit=2", headers=bearer("viewer")
    )
    assert [e["line"] for e in second.json()["entries"]] == ["three"]
    assert (
        env.client.get(f"{base}/logs?limit=501", headers=bearer("viewer")).status_code
        == 422
    )
    assert env.client.post(f"{base}/lease", headers=bearer("editor")).status_code == 200
    start = time.monotonic()
    assert env.client.post(f"{base}/stop", headers=bearer("editor")).status_code == 200
    assert time.monotonic() - start < 20


def test_route_stop_with_real_backend_waits_for_pods_gone(env: SimpleNamespace) -> None:
    class DisappearingPods(FakeKube):
        polls = 0

        async def list_namespaced_pod(self, namespace: str, label_selector: str):
            self.polls += 1
            if self.polls == 2:
                self.pods.clear()
            return await super().list_namespaced_pod(namespace, label_selector)

    kube = DisappearingPods()
    kube.pods = [
        {
            "metadata": {
                "name": "agent-pod",
                "labels": {"factory.io/investigation": env.run_id},
            }
        }
    ]
    kube.jobs["late-unprefixed"] = {
        "metadata": {
            "name": "late-unprefixed",
            "uid": "late",
            "labels": {"factory.io/investigation": env.run_id},
        },
        "status": {},
    }
    backend = InvestigationBackend(
        env.backend.store, batch=kube, core=kube, networking=kube
    )
    env.client.app.state.investigation_backend = backend
    start = time.monotonic()
    response = env.client.post(
        f"/api/investigations/{env.run_id}/stop", headers=bearer("editor")
    )
    assert response.status_code == 200
    assert response.json() == {"state": "stopped", "pods_gone": True}
    assert time.monotonic() - start < 20
    assert kube.polls == 2
    assert not kube.jobs
    assert asyncio.run(backend.store.get(env.run_id)).pods_gone


def test_create_and_stdio_proxy(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_dispatch(**kwargs: object) -> dict[str, str]:
        assert kwargs["project_id"] == env.project_id
        assert kwargs["repo_path"] == env.tmp_path
        assert kwargs["extra_patterns"] == "*.secret"
        return {"id": str(uuid4()), "state": "running"}

    service = SimpleNamespace(
        _resolve_claude_token_pooled=lambda _key: ("claude-token", None, None),
        _release_task_credential=lambda _key: None,
    )
    monkeypatch.setattr(investigations, "get_agent_service", lambda: service)
    monkeypatch.setattr(
        investigations.project_settings_store,
        "get_settings",
        AsyncMock(return_value={"INVESTIGATE_SECRET_PATHS": "*.secret"}),
    )
    env.backend.dispatch = AsyncMock(side_effect=fake_dispatch)
    body = {"project_id": env.project_id, "sha": "a" * 40, "request": "find issue"}
    assert (
        env.client.post(
            "/api/investigations", json=body, headers=bearer("viewer")
        ).status_code
        == 403
    )
    assert (
        env.client.post(
            "/api/investigations", json=body, headers=bearer("editor")
        ).status_code
        == 200
    )
    proxy = env.client.get(
        f"/api/mcp-stdio/investigations/{env.run_id}", headers=bearer("test-token")
    )
    assert proxy.status_code == 200
    assert proxy.json()["state"] == "running"


def test_stdio_create_invalid_body_returns_422(env: SimpleNamespace) -> None:
    assert (
        env.client.post(
            "/api/mcp-stdio/investigations", json={}, headers=bearer("test-token")
        ).status_code
        == 422
    )


def test_refused_second_create_keeps_first_credential(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked_out: set[str] = set()

    def checkout(key: str) -> tuple[str, None, None]:
        checked_out.add(key)
        return ("claude-token", None, None)

    service = SimpleNamespace(
        _resolve_claude_token_pooled=checkout,
        _release_task_credential=checked_out.discard,
    )
    monkeypatch.setattr(investigations, "get_agent_service", lambda: service)
    monkeypatch.setattr(
        investigations.project_settings_store,
        "get_settings",
        AsyncMock(return_value={}),
    )
    first_id = str(uuid4())
    env.backend.dispatch = AsyncMock(
        side_effect=[
            {"id": first_id, "state": "running"},
            investigations.InvestigationRefused("project investigation slot taken"),
        ]
    )
    body = {"project_id": env.project_id, "sha": "a" * 40, "request": "find issue"}
    assert (
        env.client.post(
            "/api/investigations", json=body, headers=bearer("editor")
        ).status_code
        == 200
    )
    first_key = env.client.app.state.investigation_credential_keys[first_id]
    assert (
        env.client.post(
            "/api/investigations", json=body, headers=bearer("editor")
        ).status_code
        == 409
    )
    assert first_key in checked_out
