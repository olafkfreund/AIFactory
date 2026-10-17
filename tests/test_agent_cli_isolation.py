"""Agent CLI and tool subprocesses receive a private HOME and filtered env."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, NoReturn

import pytest
from providers import codex
from tools.executor import ToolExecutor


async def test_codex_spawn_uses_private_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "real"
    (real_home / ".codex").mkdir(parents=True)
    (real_home / ".codex" / "auth.json").write_text("{}")
    (real_home / ".ssh").mkdir()
    (real_home / ".ssh" / "id_ed25519").write_text("fake key")
    monkeypatch.setenv("HOME", str(real_home))
    for key in ("CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "socket"))
    monkeypatch.setenv("FOO_SECRETISH", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/codex")

    captured: dict[str, str] = {}

    async def fake_spawn(*_args: object, **kwargs: Any) -> NoReturn:
        captured.update(kwargs["env"])
        private_home = Path(captured["HOME"])
        assert private_home != real_home
        assert (private_home / ".codex" / "auth.json").read_text() == "{}"
        assert not (private_home / ".ssh").exists()
        raise RuntimeError("spawn captured")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    provider = codex.CodexCLIProvider()
    await provider.query("hello")
    with pytest.raises(RuntimeError, match="spawn captured"):
        await provider.receive_response().__anext__()

    assert "SSH_AUTH_SOCK" not in captured
    assert "FOO_SECRETISH" not in captured
    assert captured["OPENAI_API_KEY"] == "fake"


async def test_bash_tool_uses_private_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "real"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "socket"))
    result = await ToolExecutor(tmp_path).execute(
        "Bash", {"command": 'printf \'%s|%s\' "$HOME" "${SSH_AUTH_SOCK:-none}"'}
    )
    assert not result.is_error, result.content
    assert isinstance(result.content, str)
    home, socket = result.content.split("|")
    assert home != str(real_home)
    assert Path(home).name.startswith("aif-home-")
    assert socket == "none"
