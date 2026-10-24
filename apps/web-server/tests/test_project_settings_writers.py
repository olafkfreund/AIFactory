"""Tests for project settings writers and read-back (#1795)."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
import server.routes.context as context_routes
import server.routes.github as github_routes
import server.routes.projects as projects_routes
import server.services.project_settings_store as pss
from server.crypto import kms
from server.database.models import Base
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_kms_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    monkeypatch.delenv("KMS_FERNET_KEY", raising=False)
    monkeypatch.delenv("APP_KMS_FERNET_KEY", raising=False)
    monkeypatch.delenv("APP_KMS_BACKEND", raising=False)
    monkeypatch.delenv("KMS_BACKEND", raising=False)
    kms._INSTANCE = None
    pss._SNAPSHOT.clear()
    pss._WARNED_REAPPEARED.clear()
    yield
    kms._INSTANCE = None
    pss._SNAPSHOT.clear()
    pss._WARNED_REAPPEARED.clear()


@pytest_asyncio.fixture(autouse=True)
async def _bind_temp_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[None, None]:
    db_file = tmp_path / "writers_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(pss, "_SESSION_FACTORY", factory)
    try:
        yield
    finally:
        await engine.dispose()


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    registry: dict[str, dict[str, Any]] = {
        "proj-1": {
            "id": "proj-1",
            "name": "Test Project",
            "path": str(p),
            "settings": {},
        }
    }
    monkeypatch.setattr(projects_routes, "load_projects", lambda: registry)
    monkeypatch.setattr(projects_routes, "save_projects", lambda _p: None)
    monkeypatch.setattr("server.project_registry.load_projects", lambda: registry)
    monkeypatch.setattr("server.project_registry.save_projects", lambda _p: None)
    return p


async def test_patch_settings_stores_auto_merge_and_creates_no_env_file(
    project_dir: Path,
) -> None:
    update = projects_routes.ProjectSettingsUpdate(autoMerge=True)
    res = await projects_routes.update_project_settings(
        project_id="proj-1",
        settings=update,
        _access={"role": "admin"},
    )
    assert res["success"] is True
    assert not (project_dir / ".aifactory" / ".env").exists()

    stored = await pss.get_settings(project_dir)
    assert stored.get("AIFACTORY_AUTO_MERGE") == "true"


async def test_patch_settings_stores_data_classification(project_dir: Path) -> None:
    await projects_routes.update_project_settings(
        project_id="proj-1",
        settings=projects_routes.ProjectSettingsUpdate(dataClassification="regulated"),
        _access={"role": "admin"},
    )
    stored = await pss.get_settings(project_dir)
    assert stored.get("AIFACTORY_DATA_CLASSIFICATION") == "regulated"
    assert await pss.data_classification(project_dir) == "regulated"


def test_unknown_data_classification_is_rejected() -> None:
    with pytest.raises(ValidationError):
        projects_routes.ProjectSettingsUpdate(dataClassification="secret")


async def test_patch_env_mcp_fields_round_trip_through_get_env(
    project_dir: Path,
) -> None:
    custom_servers = [
        {
            "id": "my-mcp",
            "name": "My MCP",
            "type": "command",
            "command": "npx",
            "args": ["-y", "my-mcp-server"],
        }
    ]
    agent_overrides = {
        "coder": {"add": ["my-mcp"], "remove": ["context7"]},
        "planner": {"add": ["my-mcp"]},
    }
    mcp_servers = {"context7Enabled": False, "playwrightEnabled": True}

    patch_res = await context_routes.update_project_env(
        projectId="proj-1",
        config=context_routes.ProjectEnvUpdate(
            mcpServers=mcp_servers,
            agentMcpOverrides=agent_overrides,
            customMcpServers=custom_servers,
        ),
    )
    assert patch_res["success"] is True
    assert not (project_dir / ".aifactory" / ".env").exists()

    get_res = await context_routes.get_project_env(projectId="proj-1")
    assert get_res["success"] is True
    data = get_res["data"]
    assert data["mcpServers"] == {"context7Enabled": False, "playwrightEnabled": True}
    assert data["agentMcpOverrides"] == agent_overrides
    assert data["customMcpServers"] == custom_servers

    # Updating agentMcpOverrides replaces old AGENT_MCP_* keys, and an empty
    # customMcpServers list deletes CUSTOM_MCP_SERVERS.
    clear_res = await context_routes.update_project_env(
        projectId="proj-1",
        config=context_routes.ProjectEnvUpdate(
            agentMcpOverrides={"qa_reviewer": {"remove": ["playwright"]}},
            customMcpServers=[],
        ),
    )
    assert clear_res["success"] is True

    get_after = await context_routes.get_project_env(projectId="proj-1")
    data_after = get_after["data"]
    assert data_after["agentMcpOverrides"] == {
        "qa_reviewer": {"remove": ["playwright"]}
    }
    assert "customMcpServers" not in data_after
    stored = await pss.get_settings(project_dir)
    assert "CUSTOM_MCP_SERVERS" not in stored
    assert "AGENT_MCP_coder_ADD" not in stored
    assert "AGENT_MCP_planner_ADD" not in stored


@pytest.mark.usefixtures("project_dir")
async def test_patch_env_rejects_invalid_agent_name_in_overrides() -> None:
    res = await context_routes.update_project_env(
        projectId="proj-1",
        config=context_routes.ProjectEnvUpdate(
            agentMcpOverrides={"bad-agent!": {"add": ["my-mcp"]}},
        ),
    )
    body = json.loads(res.body) if hasattr(res, "body") else res
    assert body["success"] is False
    assert "error" in body


async def test_get_env_reports_legacy_settings_file(project_dir: Path) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env.migrated-2026-10-14").write_text("MAIN_BRANCH=old\n")
    newest = env_dir / ".env.migrated-2026-10-15"
    newest.write_text("MAIN_BRANCH=main\n")

    res = await context_routes.get_project_env(projectId="proj-1")
    assert res["success"] is True
    assert res["data"]["legacySettingsFile"] == ".env.migrated-2026-10-15"


async def test_gh_token_persist_stores_github_token(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        github_routes,
        "run_gh_command",
        lambda _args: {"success": True, "output": "ghp_valid_cli_token_value"},
    )

    ok = await github_routes._persist_cli_token_to_project("proj-1")
    assert ok is True
    assert not (project_dir / ".aifactory" / ".env").exists()

    stored = await pss.get_settings(project_dir)
    assert stored.get("GITHUB_TOKEN") == "ghp_valid_cli_token_value"
