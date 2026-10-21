"""Tool-loop Bash/Grep spawns get a scrubbed env (#1713)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NoReturn

import pytest
import tools.executor
from tools.executor import ToolExecutor

_SECRETS = (
    "APP_API_TOKEN",
    "app_api_token",
    "OLLAMA_API_KEY",
    "AIFACTORY_TRUSTED_CONTRACT",
    "GITHUB_TOKEN",
    "NPM_TOKEN",
)


class _SpawnedError(Exception):
    pass


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [("Bash", {"command": "echo hi"}), ("Grep", {"pattern": "x"})],
)
async def test_spawn_env_is_scrubbed(
    name: str,
    tool_input: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _allow(*_a: object, **_kw: object) -> dict[str, Any]:
        return {}

    monkeypatch.setattr("security.hooks.bash_security_hook", _allow)
    executor = ToolExecutor(tmp_path)
    # Set after construction: the env must be read at spawn time.
    for key in _SECRETS:
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", os.environ["PATH"])
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.invalid")

    captured: dict[str, str] = {}

    async def _fake(*_a: object, **kw: Any) -> NoReturn:
        captured.update(kw["env"])
        raise _SpawnedError

    monkeypatch.setattr(tools.executor.asyncio, "create_subprocess_shell", _fake)
    monkeypatch.setattr(tools.executor.asyncio, "create_subprocess_exec", _fake)

    # execute() swallows exceptions into an error block, so assert on captured.
    await executor.execute(name, tool_input)

    assert captured, "spawn was not reached"
    for key in _SECRETS:
        assert not captured.get(key), key
    assert captured.get("PATH")
    assert "OLLAMA_BASE_URL" not in captured
    if name == "Bash":
        assert captured["HOME"] != str(tmp_path)
    else:
        assert "HOME" not in captured
    assert any(
        k.startswith("GIT_CONFIG_KEY_") and v == "core.hooksPath"
        for k, v in captured.items()
    )
