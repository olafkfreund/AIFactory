"""Unit tests for server.services.project_settings_store (#1795)."""

from __future__ import annotations

import base64
import json
import logging
import subprocess
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from server.crypto import kms, secret_field
from server.database.models import Base, ProjectSettings
from server.services import project_settings_store as pss
from server.services.project_settings_store import (
    ENV,
    ProjectSettingsStore,
    cached_setting,
    project_key,
)
from sqlalchemy import update
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
        "LD_PRELOAD=/opt/evil.so\n",
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
    assert "/opt/evil.so" not in caplog.text


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)  # noqa: S603, S607


async def test_git_tracked_file_is_not_imported_and_not_renamed(
    store: ProjectSettingsStore,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _git("init", str(project_dir))
    _git("-C", str(project_dir), "config", "user.email", "test@example.com")
    _git("-C", str(project_dir), "config", "user.name", "Test")
    env_dir = project_dir / ".aifactory"
    env_dir.mkdir()
    env_file = env_dir / ".env"
    content = "AIFACTORY_AUTO_MERGE=true\nUSE_CLAUDE_MD=true\n"
    env_file.write_text(content, encoding="utf-8")
    _git("-C", str(project_dir), "add", "-f", ".aifactory/.env")

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
            project_dir, {"USE_CLAUDE_MD": "true", "LD_PRELOAD": "/opt/x.so"}
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

    sentinel = "ghp_super_sentinel_value_1795"
    await store.put_settings(project_dir, {"GITHUB_TOKEN": sentinel})

    async with session_factory() as session:
        row = await session.get(ProjectSettings, project_key(project_dir))
        assert row is not None
        assert row.settings.startswith("enc.v1:")
        assert sentinel not in row.settings
        assert "GITHUB_TOKEN" not in row.settings

    assert await store.get_settings(project_dir) == {"GITHUB_TOKEN": sentinel}


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


def _set_fernet_key(monkeypatch: pytest.MonkeyPatch, seed: bytes) -> None:
    monkeypatch.setenv("KMS_FERNET_KEY", base64.urlsafe_b64encode(seed * 32).decode())
    kms._INSTANCE = None


async def _row(
    session_factory: async_sessionmaker[AsyncSession], project_dir: Path
) -> ProjectSettings | None:
    async with session_factory() as session:
        return await session.get(ProjectSettings, project_key(project_dir))


async def test_unreadable_legacy_file_stores_nothing_and_retries(
    store: ProjectSettingsStore,
    session_factory: async_sessionmaker[AsyncSession],
    project_dir: Path,
) -> None:
    """A read error must not commit an empty row or rename the file."""
    env_file = project_dir / ".aifactory" / ".env"
    env_file.parent.mkdir()
    env_file.write_bytes(b"USE_CLAUDE_MD=\xff\xfe\n")

    with pytest.raises(pss.ProjectSettingsUnavailableError):
        await store.get_settings(project_dir)
    assert await _row(session_factory, project_dir) is None
    assert env_file.exists()

    env_file.write_text("USE_CLAUDE_MD=true\n", encoding="utf-8")
    assert await store.get_settings(project_dir) == {"USE_CLAUDE_MD": "true"}
    assert not env_file.exists()


async def test_undecryptable_row_raises_and_is_not_overwritten(
    store: ProjectSettingsStore,
    session_factory: async_sessionmaker[AsyncSession],
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong or rotated key fails loudly; the sealed row stays recoverable."""
    _set_fernet_key(monkeypatch, b"a")
    await store.put_settings(project_dir, {"GITHUB_TOKEN": "tok-1795-abc"})
    before = (await _row(session_factory, project_dir)).settings  # type: ignore[union-attr]
    pss._SNAPSHOT[project_key(project_dir)] = {"GITHUB_TOKEN": "tok-1795-abc"}

    _set_fernet_key(monkeypatch, b"b")
    with pytest.raises(pss.ProjectSettingsUnavailableError):
        await store.get_settings(project_dir)
    assert pss._SNAPSHOT[project_key(project_dir)] == {"GITHUB_TOKEN": "tok-1795-abc"}
    assert (await _row(session_factory, project_dir)).settings == before  # type: ignore[union-attr]


async def test_plaintext_row_is_sealed_on_read_once_a_key_exists(
    store: ProjectSettingsStore,
    session_factory: async_sessionmaker[AsyncSession],
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await store.put_settings(project_dir, {"GITHUB_TOKEN": "plain-17"})
    assert not (await _row(session_factory, project_dir)).settings.startswith("enc.v1:")  # type: ignore[union-attr]

    _set_fernet_key(monkeypatch, b"c")
    assert await store.get_settings(project_dir) == {"GITHUB_TOKEN": "plain-17"}
    raw = (await _row(session_factory, project_dir)).settings  # type: ignore[union-attr]
    assert raw.startswith("enc.v1:")
    assert "plain-17" not in raw


async def test_reseal_does_not_overwrite_a_newer_save(
    store: ProjectSettingsStore,
    session_factory: async_sessionmaker[AsyncSession],
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A save committed between the read and the re-seal wins."""
    await store.put_settings(project_dir, {"GITHUB_TOKEN": "plain-17"})
    _set_fernet_key(monkeypatch, b"d")
    newer = secret_field.seal(json.dumps({"GITHUB_TOKEN": "newer-17"}))
    assert newer is not None
    real_reseal = pss._reseal_if_unchanged

    async def racing_reseal(session: Any, key: str, raw: str, resealed: str) -> None:
        await session.execute(
            update(ProjectSettings)
            .where(ProjectSettings.project_key == key)
            .values(settings=newer)
        )
        await session.commit()
        await real_reseal(session, key, raw, resealed)

    monkeypatch.setattr(pss, "_reseal_if_unchanged", racing_reseal)
    assert await store.get_settings(project_dir) == {"GITHUB_TOKEN": "plain-17"}
    assert (await _row(session_factory, project_dir)).settings == newer  # type: ignore[union-attr]
    monkeypatch.setattr(pss, "_reseal_if_unchanged", real_reseal)
    assert await store.get_settings(project_dir) == {"GITHUB_TOKEN": "newer-17"}


@pytest.mark.parametrize(
    "fault",
    [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 10), 1],
    ids=["git-missing", "timeout", "exit-1"],
)
async def test_git_check_failure_defers_the_import(
    store: ProjectSettingsStore,
    session_factory: async_sessionmaker[AsyncSession],
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: BaseException | int,
) -> None:
    """The tracked-file check fails closed: no row, no rename, retry later."""
    env_file = project_dir / ".aifactory" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("USE_CLAUDE_MD=true\n", encoding="utf-8")

    def broken_run(*_args: object, **_kwargs: object) -> object:
        if isinstance(fault, BaseException):
            raise fault
        return subprocess.CompletedProcess([], fault, "", "")

    monkeypatch.setattr(subprocess, "run", broken_run)
    with pytest.raises(pss.ProjectSettingsUnavailableError):
        await store.get_settings(project_dir)
    assert await _row(session_factory, project_dir) is None
    assert env_file.exists()


async def test_not_a_git_repo_counts_as_untracked(
    store: ProjectSettingsStore, project_dir: Path
) -> None:
    env_file = project_dir / ".aifactory" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("USE_CLAUDE_MD=true\n", encoding="utf-8")
    assert await store.get_settings(project_dir) == {"USE_CLAUDE_MD": "true"}


def test_git_check_disables_fsmonitor_and_hooks(
    project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert pss._is_tracked(project_dir) is False
    assert "core.fsmonitor=false" in seen[0]
    assert "core.hooksPath=/dev/null" in seen[0]
