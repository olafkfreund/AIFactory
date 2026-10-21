"""Call-site regression tests for per-project settings loading (#1745, #1795)."""

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from server import project_registry
from server.services import (
    agent_service,
    agent_spec_creation,
    pr_review_service,
    project_settings_store,
    trusted_contract,
    trusted_contract_store,
)
from server.services.agent_service import AgentService
from server.services.pr_review_service import PRReviewService

_SEEDED_SETTINGS = {
    "USE_CLAUDE_MD": "true",
    "LD_PRELOAD": "x",
    "NODE_OPTIONS": "x",
    "BASH_ENV": "x",
}
_REFUSED_KEYS = ("LD_PRELOAD", "NODE_OPTIONS", "BASH_ENV")


class _SentinelError(Exception):
    """Raised by the fake create_subprocess_exec so no real process runs."""


def _prepare_project_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[dict[str, str]]]:
    project_path = tmp_path / "project"
    project_path.mkdir(parents=True)

    async def fake_get_settings(_path: str | Path, *_args: Any) -> dict[str, str]:
        return dict(_SEEDED_SETTINGS)

    monkeypatch.setattr(project_settings_store, "get_settings", fake_get_settings)

    for key in ("USE_CLAUDE_MD", project_settings_store.ENV, *_REFUSED_KEYS):
        monkeypatch.delenv(key, raising=False)

    recorded_envs: list[dict[str, str]] = []
    return project_path, recorded_envs


def _make_fake_exec(recorded_envs: list[dict[str, str]]) -> Any:
    async def fake_create_subprocess_exec(*_args: Any, **kwargs: Any) -> Any:
        stdin_fd = kwargs.get("stdin")
        if isinstance(stdin_fd, int):
            with contextlib.suppress(OSError):
                os.close(stdin_fd)
        recorded_envs.append(dict(kwargs["env"]))
        raise _SentinelError("stop before spawning subprocess")

    return fake_create_subprocess_exec


def _assert_filtered_env(recorded_envs: list[dict[str, str]]) -> None:
    assert len(recorded_envs) == 1
    env = recorded_envs[0]
    assert env.get("USE_CLAUDE_MD") == "true"
    assert env.get(project_settings_store.ENV) == "{}"
    for key in _REFUSED_KEYS:
        assert key not in env


@pytest.mark.asyncio
async def test_pr_review_service_start_review_filters_project_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, recorded_envs = _prepare_project_env(tmp_path, monkeypatch)

    backend_dir = tmp_path / "backend"
    runner_script = backend_dir / "runners" / "github" / "runner.py"
    runner_script.parent.mkdir(parents=True)
    runner_script.write_text("# stub runner\n", encoding="utf-8")

    monkeypatch.setattr(
        pr_review_service,
        "get_settings",
        lambda: SimpleNamespace(BACKEND_PATH=str(backend_dir)),
    )
    monkeypatch.setattr(
        pr_review_service.asyncio,
        "create_subprocess_exec",
        _make_fake_exec(recorded_envs),
    )

    service = PRReviewService()

    async def fake_emit_error(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(service, "_emit_error", fake_emit_error)

    with contextlib.suppress(_SentinelError):
        await service.start_review(
            project_id="proj-1",
            pr_number=42,
            project_path=project_path,
        )

    _assert_filtered_env(recorded_envs)


@pytest.mark.asyncio
async def test_store_failure_fails_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1795: a DB error fails the start; no child runs with empty settings."""
    project_path, recorded_envs = _prepare_project_env(tmp_path, monkeypatch)

    async def broken_get_settings(*_args: Any) -> dict[str, str]:
        raise RuntimeError("database is down")

    monkeypatch.setattr(project_settings_store, "get_settings", broken_get_settings)
    backend_dir = tmp_path / "backend"
    runner_script = backend_dir / "runners" / "github" / "runner.py"
    runner_script.parent.mkdir(parents=True)
    runner_script.write_text("# stub runner\n", encoding="utf-8")
    monkeypatch.setattr(
        pr_review_service,
        "get_settings",
        lambda: SimpleNamespace(BACKEND_PATH=str(backend_dir)),
    )
    monkeypatch.setattr(
        pr_review_service.asyncio,
        "create_subprocess_exec",
        _make_fake_exec(recorded_envs),
    )

    with pytest.raises(
        project_settings_store.ProjectSettingsUnavailableError,
        match="project settings unavailable: database error",
    ):
        await PRReviewService().start_review(
            project_id="proj-1", pr_number=42, project_path=project_path
        )
    assert recorded_envs == []


@pytest.mark.asyncio
async def test_agent_service_spawn_task_execution_filters_project_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, recorded_envs = _prepare_project_env(tmp_path, monkeypatch)
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True)

    service = AgentService()
    monkeypatch.setattr(service.settings, "BACKEND_PATH", backend_dir)

    monkeypatch.setattr(service, "_write_skill_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        service, "_resolve_claude_token_pooled", lambda *_a, **_kw: (None, None, None)
    )
    monkeypatch.setattr(project_registry, "load_projects", lambda: {})

    async def fake_stamp_spawn(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_spawn_env(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(trusted_contract_store, "stamp_spawn", fake_stamp_spawn)
    monkeypatch.setattr(trusted_contract, "spawn_env", fake_spawn_env)
    monkeypatch.setattr(
        agent_service.asyncio,
        "create_subprocess_exec",
        _make_fake_exec(recorded_envs),
    )

    with contextlib.suppress(_SentinelError):
        await service._spawn_task_execution(
            task_id="proj-1:001",
            project_path=project_path,
            spec_id="001",
        )

    _assert_filtered_env(recorded_envs)


@pytest.mark.asyncio
async def test_agent_service_start_spec_creation_filters_project_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, recorded_envs = _prepare_project_env(tmp_path, monkeypatch)
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True)

    service = AgentService()
    monkeypatch.setattr(service.settings, "BACKEND_PATH", backend_dir)

    monkeypatch.setattr(service, "_write_skill_context", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        service, "_resolve_claude_token_pooled", lambda *_a, **_kw: (None, None, None)
    )
    monkeypatch.setattr(service, "_kubejob_backend_enabled", lambda: False)
    monkeypatch.setattr(
        agent_spec_creation.asyncio,
        "create_subprocess_exec",
        _make_fake_exec(recorded_envs),
    )

    with contextlib.suppress(_SentinelError):
        await service.start_spec_creation(
            task_id="proj-1:001",
            project_path=project_path,
            title="Test spec",
            description="Test description",
        )

    _assert_filtered_env(recorded_envs)
