"""The strict investigation switch controls route mounting and the loop."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server.main import create_app
from server.services import build_backend
from server.services.investigation_backend import investigations_enabled


def test_switch_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFACTORY_INVESTIGATIONS", raising=False)
    assert investigations_enabled() is False
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "off")
    assert investigations_enabled() is False
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "on")
    assert investigations_enabled() is True
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "true")
    with pytest.raises(ValueError, match="AIFACTORY_INVESTIGATIONS"):
        investigations_enabled()


def test_router_and_loop_only_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "off")
    off = create_app()
    assert not any(r.path.startswith("/api/investigations") for r in off.routes)
    assert not any(
        r.path.startswith("/api/mcp-stdio/investigations") for r in off.routes
    )
    assert not hasattr(off.state, "investigation_backend")
    client = TestClient(off)
    run_id = "a" * 36
    for method, path in (
        ("POST", "/api/investigations"),
        ("GET", f"/api/investigations/{run_id}"),
        ("GET", f"/api/investigations/{run_id}/logs"),
        ("POST", f"/api/investigations/{run_id}/lease"),
        ("POST", f"/api/investigations/{run_id}/stop"),
        ("POST", f"/api/investigations/{run_id}/pod/log"),
        ("POST", f"/api/investigations/{run_id}/pod/run-tests"),
        ("POST", f"/api/investigations/{run_id}/pod/report"),
        ("GET", f"/api/investigations/{run_id}/pod/lease"),
        ("GET", f"/api/investigations/{run_id}/pod/config"),
        ("GET", f"/api/mcp-stdio/investigations/{run_id}"),
    ):
        assert client.request(method, path).status_code == 404

    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "on")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setattr(build_backend, "kubejob_enabled", lambda: True)
    on = create_app()
    assert len([r for r in on.routes if r.path.startswith("/api/investigations")]) == 10
    assert (
        len(
            [r for r in on.routes if r.path.startswith("/api/mcp-stdio/investigations")]
        )
        == 5
    )
    assert on.state.investigation_backend._loop is None
