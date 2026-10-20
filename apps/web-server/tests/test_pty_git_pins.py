"""Tests for git config pins in web terminal PTY sessions (#1745)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("ptyprocess")

from server.pty import session

_TOKEN = "t"  # noqa: S105 - placeholder, not a credential


def _git_config(env: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]


def _value(env: dict[str, str], key: str) -> str:
    matches = [v for k, v in _git_config(env) if k == key]
    assert matches, f"{key!r} not pinned"
    return matches[-1]


def _start(monkeypatch: pytest.MonkeyPatch, home: Path) -> dict[str, str]:
    captured: dict[str, dict[str, str]] = {}

    def fake_spawn(*_args: object, env: dict[str, str], **_kw: object) -> object:
        captured["env"] = dict(env)
        return object()

    monkeypatch.setattr(session.PtyProcessUnicode, "spawn", fake_spawn)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("EDITOR", "nano")
    for var in (
        "VISUAL",
        "PAGER",
        "GIT_PAGER",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
    ):
        monkeypatch.delenv(var, raising=False)
    session.PTYSession(env={"CLAUDE_CODE_OAUTH_TOKEN": _TOKEN}).start()
    return captured["env"]


def test_pty_session_pins_git_config_preserving_user_editor_and_pager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _start(monkeypatch, tmp_path)
    assert _value(env, "core.editor") == "nano"
    assert _value(env, "sequence.editor") == "nano"
    assert _value(env, "core.pager") == "less"
    assert env["GIT_PAGER"] == "less"
    assert _value(env, "core.hooksPath") == "/dev/null"
    assert _value(env, "credential.helper") == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _TOKEN


def test_pty_session_restores_the_users_global_credential_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gitconfig = tmp_path / ".gitconfig"
    for key, value in (
        ("credential.helper", "store"),
        ("credential.https://gitlab.example.helper", "!glab auth git-credential"),
    ):
        subprocess.run(  # noqa: S603
            ["git", "config", "--file", str(gitconfig), "--add", key, value],  # noqa: S607
            check=True,
        )
    pins = _git_config(_start(monkeypatch, tmp_path))
    reset = pins.index(("credential.helper", ""))
    # The user's own helpers come back after the reset, in their config order.
    assert pins[reset + 1 :][-2:] == [
        ("credential.helper", "store"),
        ("credential.https://gitlab.example.helper", "!glab auth git-credential"),
    ]
