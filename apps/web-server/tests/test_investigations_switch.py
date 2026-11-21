"""The strict investigation switch controls route mounting and the loop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from server import main
from server.config import get_settings
from server.main import create_app
from server.services import build_backend, github_app
from server.services.investigation_backend import investigations_enabled
from server.utils import subprocess_env


def test_switch_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFACTORY_INVESTIGATIONS", raising=False)
    assert investigations_enabled() is False
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "off")
    assert investigations_enabled() is False
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "on")
    assert investigations_enabled() is True
    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "drain")
    assert investigations_enabled() is True
    for invalid in ("true", "Drain", "draining"):
        monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", invalid)
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

    monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", "drain")
    drain = create_app()
    assert (
        len(
            [
                r
                for r in drain.routes
                if getattr(r, "path", "").startswith("/api/investigations")
            ]
        )
        == 10
    )
    assert (
        len(
            [
                r
                for r in drain.routes
                if getattr(r, "path", "").startswith("/api/mcp-stdio/investigations")
            ]
        )
        == 5
    )


def test_startup_starts_loop_in_drain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class StartupReachedError(Exception):
        pass

    async def init_db() -> None:
        return None

    monkeypatch.setattr(main, "_make_non_dumpable", lambda: None)
    monkeypatch.setattr(github_app, "start", lambda: None)
    monkeypatch.setattr(github_app, "warn_legacy_web_login", lambda: None)
    monkeypatch.setattr(github_app, "load_web_login", lambda: None)
    monkeypatch.setattr(main, "init_db", init_db)
    monkeypatch.setattr(
        main, "init_skills_service", Mock(side_effect=StartupReachedError)
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "PROJECTS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "BACKEND_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(subprocess_env, "sweep_github_dirs", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setattr(build_backend, "kubejob_enabled", lambda: True)
    for value, expected in (("off", 0), ("drain", 1)):
        monkeypatch.setenv("AIFACTORY_INVESTIGATIONS", value)
        app = create_app()
        start = Mock()
        if value == "drain":
            app.state.investigation_backend.start_loop = start
        with pytest.raises(StartupReachedError):
            asyncio.run(main.lifespan(app).__aenter__())
        assert start.call_count == expected
