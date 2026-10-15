"""Unit tests for server.services.project_settings_store (#1795)."""

from __future__ import annotations

import base64
import json
import logging
import subprocess
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from server.crypto import kms
from server.database.models import Base, ProjectSettings
from server.services import project_settings_store as pss
from server.services.project_settings_store import (
    ENV,
    ProjectSettingsStore,
    cached_setting,
    project_key,
)
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


@pytest_asyncio.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    db_file = tmp_path / "test_project_settings.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> ProjectSettingsStore:
    return ProjectSettingsStore(session_factory)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


async def test_import_keeps_only_store_keys(
    store: ProjectSettingsStore,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "# comment\n"
        'USE_CLAUDE_MD="true"\n'
        "MAIN_BRANCH='develop'\n"
        'CUSTOM_MCP_SERVERS=[{"id":"s1"}]\n'
        "AGENT_MCP_coder_ADD=context7\n"
        "LD_PRELOAD=sentinel-secret-preload\n"
        "NODE_OPTIONS=--inspect\n"
        "NOT_A_STORE_KEY=sentinel-secret-other\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger=pss.logger.name):
        settings = await store.get_settings(project_dir)

    assert settings == {
        "USE_CLAUDE_MD": "true",
        "MAIN_BRANCH": "develop",
        "CUSTOM_MCP_SERVERS": '[{"id":"s1"}]',
        "AGENT_MCP_coder_ADD": "context7",
    }
    assert "LD_PRELOAD" in caplog.text
    assert "NODE_OPTIONS" in caplog.text
    assert "NOT_A_STORE_KEY" in caplog.text
    assert "sentinel-secret-preload" not in caplog.text
    assert "sentinel-secret-other" not in caplog.text


async def test_import_renames_file_and_preserves_content(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    original = "USE_CLAUDE_MD=true\nGITHUB_TOKEN=ghp_legacy_plain\n"
    env_file.write_text(original, encoding="utf-8")

    await store.get_settings(project_dir)

    assert not env_file.exists()
    migrated = list(env_dir.glob(".env.migrated-*"))
    assert len(migrated) == 1
    assert migrated[0].read_text(encoding="utf-8") == original


async def test_second_get_settings_does_not_reimport(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    env_file.write_text("MAIN_BRANCH=main\n", encoding="utf-8")

    first = await store.get_settings(project_dir)
    assert first == {"MAIN_BRANCH": "main"}

    # Even if a new .env file is written with different values, the stored row wins.
    env_file.write_text(
        "MAIN_BRANCH=hacked\nAIFACTORY_AUTO_MERGE=true\n", encoding="utf-8"
    )
    second = await store.get_settings(project_dir)
    assert second == {"MAIN_BRANCH": "main"}


async def test_reappearing_file_is_ignored_and_logs_keys_only(
    store: ProjectSettingsStore,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # First read with no file creates the empty row.
    assert await store.get_settings(project_dir) == {}

    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "AIFACTORY_AUTO_MERGE=true\n"
        "GITHUB_TOKEN=ghp_top_secret_value_1795\n"
        "LD_PRELOAD=/tmp/evil.so\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger=pss.logger.name):
        settings = await store.get_settings(project_dir)
        # Calling again in the same process warns only once for this project.
        await store.get_settings(project_dir)

    assert settings == {}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "AIFACTORY_AUTO_MERGE" in warnings[0]
    assert "GITHUB_TOKEN" in warnings[0]
    assert "LD_PRELOAD" in warnings[0]
    assert "ghp_top_secret_value_1795" not in caplog.text
    assert "/tmp/evil.so" not in caplog.text


async def test_git_tracked_file_is_not_imported_and_not_renamed(
    store: ProjectSettingsStore,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subprocess.run(["git", "init", str(project_dir)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project_dir), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(project_dir), "config", "user.name", "Test"],
        check=True,
    )
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    content = "AIFACTORY_AUTO_MERGE=true\nUSE_CLAUDE_MD=true\n"
    env_file.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project_dir), "add", "-f", ".aifactory/.env"],
        check=True,
    )

    with caplog.at_level(logging.WARNING, logger=pss.logger.name):
        settings = await store.get_settings(project_dir)

    assert settings == {}
    assert env_file.exists()
    assert env_file.read_text(encoding="utf-8") == content
    assert list(env_dir.glob(".env.migrated-*")) == []
    assert any("git-tracked" in r.getMessage() for r in caplog.records)


async def test_existing_migrated_file_is_not_overwritten(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    existing_migrated = env_dir / f".env.migrated-{date_str}"
    existing_migrated.write_text("OLD_CONTENT=1\n", encoding="utf-8")

    env_file = env_dir / ".env"
    env_file.write_text("USE_CLAUDE_MD=true\n", encoding="utf-8")

    settings = await store.get_settings(project_dir)

    assert settings == {"USE_CLAUDE_MD": "true"}
    assert not env_file.exists()
    assert existing_migrated.read_text(encoding="utf-8") == "OLD_CONTENT=1\n"
    suffixed = env_dir / f".env.migrated-{date_str}-1"
    assert suffixed.exists()
    assert suffixed.read_text(encoding="utf-8") == "USE_CLAUDE_MD=true\n"


async def test_put_settings_refuses_ld_preload(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    with pytest.raises(ValueError, match="LD_PRELOAD"):
        await store.put_settings(
            project_dir, {"USE_CLAUDE_MD": "true", "LD_PRELOAD": "/tmp/x.so"}
        )


async def test_kms_fernet_key_encrypts_raw_column(
    store: ProjectSettingsStore,
    session_factory: async_sessionmaker[AsyncSession],
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fernet_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("KMS_FERNET_KEY", fernet_key)
    kms._INSTANCE = None

    secret_token = "ghp_super_secret_token_value_1795"
    await store.put_settings(project_dir, {"GITHUB_TOKEN": secret_token})

    async with session_factory() as session:
        row = await session.get(ProjectSettings, project_key(project_dir))
        assert row is not None
        assert row.settings.startswith("enc.v1:")
        assert secret_token not in row.settings
        assert "GITHUB_TOKEN" not in row.settings

    assert await store.get_settings(project_dir) == {"GITHUB_TOKEN": secret_token}


async def test_load_project_env_skips_custom_mcp_servers_and_does_not_override(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    await store.put_settings(
        project_dir,
        {
            "USE_CLAUDE_MD": "false",
            "MAIN_BRANCH": "develop",
            "CUSTOM_MCP_SERVERS": '[{"id":"custom"}]',
        },
    )

    env: dict[str, str] = {"USE_CLAUDE_MD": "true"}
    await store.load_project_env(project_dir, env)

    assert env == {
        "USE_CLAUDE_MD": "true",
        "MAIN_BRANCH": "develop",
    }
    assert "CUSTOM_MCP_SERVERS" not in env


async def test_mcp_spawn_env_returns_only_mcp_keys(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    await store.put_settings(
        project_dir,
        {
            "GITHUB_TOKEN": "ghp_secret",
            "USE_CLAUDE_MD": "true",
            "AIFACTORY_AUTO_MERGE": "true",
            "CONTEXT7_ENABLED": "false",
            "PLAYWRIGHT_MCP_ENABLED": "true",
            "PUPPETEER_MCP_ENABLED": "false",
            "CUSTOM_MCP_SERVERS": '[{"id":"my-mcp"}]',
            "AGENT_MCP_coder_ADD": "my-mcp",
            "AGENT_MCP_qa_reviewer_REMOVE": "context7",
        },
    )

    spawn = await store.mcp_spawn_env(project_dir)
    assert list(spawn.keys()) == [ENV]
    parsed = json.loads(spawn[ENV])
    assert parsed == {
        "CONTEXT7_ENABLED": "false",
        "PLAYWRIGHT_MCP_ENABLED": "true",
        "PUPPETEER_MCP_ENABLED": "false",
        "CUSTOM_MCP_SERVERS": '[{"id":"my-mcp"}]',
        "AGENT_MCP_coder_ADD": "my-mcp",
        "AGENT_MCP_qa_reviewer_REMOVE": "context7",
    }


async def test_snapshot_and_refresh_all(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p1 = tmp_path / "p1"
    p1.mkdir()
    (p1 / ".aifactory").mkdir()
    (p1 / ".aifactory" / ".env").write_text(
        "AIFACTORY_AUTO_MERGE=true\n", encoding="utf-8"
    )

    assert cached_setting(p1, "AIFACTORY_AUTO_MERGE") is None

    monkeypatch.setattr(
        "server.project_registry.load_projects",
        lambda: {"id1": {"path": str(p1)}},
    )
    await pss.refresh_all(session_factory=session_factory)

    assert cached_setting(p1, "AIFACTORY_AUTO_MERGE") == "true"


async def test_load_project_env_filters_disallowed_keys(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "USE_CLAUDE_MD=true\nLD_PRELOAD=x\nNODE_OPTIONS=x\nBASH_ENV=x\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {}
    await store.load_project_env(project_dir, env)
    assert env == {"USE_CLAUDE_MD": "true"}


async def test_no_override_and_strips_quotes(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        'USE_CLAUDE_MD="false"\n'
        "MAIN_BRANCH='develop'\n"
        'AI_FACTORY_MODEL="claude-opus-4-6"\n',
        encoding="utf-8",
    )
    env = {"USE_CLAUDE_MD": "true"}
    await store.load_project_env(project_dir, env)
    assert env["USE_CLAUDE_MD"] == "true"
    assert env["MAIN_BRANCH"] == "develop"
    assert env["AI_FACTORY_MODEL"] == "claude-opus-4-6"


async def test_skipped_warning_logs_key_not_value(
    store: ProjectSettingsStore,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    (env_dir / ".env").write_text(
        "LD_PRELOAD=sentinel-value-1745\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {}
    with caplog.at_level(logging.WARNING, logger=pss.logger.name):
        await store.load_project_env(project_dir, env)

    assert env == {}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "LD_PRELOAD" in warnings[0]
    assert "sentinel-value-1745" not in warnings[0]


async def test_missing_file_is_noop(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    env = {"EXISTING": "1"}
    await store.load_project_env(project_dir, env)
    assert env == {"EXISTING": "1"}
