"""Tests for git config pins in web terminal PTY sessions (#1745)."""

from __future__ import annotations

import pytest

pytest.importorskip("ptyprocess")

from server.pty import session  # noqa: E402


def _git_config_value(env: dict[str, str], target_key: str) -> str:
    count = int(env["GIT_CONFIG_COUNT"])
    matches = [
        env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(count)
        if env.get(f"GIT_CONFIG_KEY_{i}") == target_key
    ]
    assert matches, f"{target_key!r} not found in GIT_CONFIG_KEY_0..{count - 1}"
    return matches[-1]


def test_pty_session_pins_git_config_preserving_user_editor_and_pager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict[str, str]] = {}

    def fake_spawn(argv, dimensions=None, cwd=None, env=None):
        captured["env"] = dict(env)
        return object()

    monkeypatch.setattr(session.PtyProcessUnicode, "spawn", fake_spawn)
    monkeypatch.setenv("EDITOR", "nano")
    for var in ("VISUAL", "PAGER", "GIT_PAGER", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(var, raising=False)

    pty = session.PTYSession(env={"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    pty.start()

    env = captured["env"]
    assert _git_config_value(env, "core.editor") == "nano"
    assert _git_config_value(env, "sequence.editor") == "nano"
    assert _git_config_value(env, "core.pager") == "less"
    assert env["GIT_PAGER"] == "less"
    assert _git_config_value(env, "core.hooksPath") == "/dev/null"
    assert _git_config_value(env, "credential.helper") == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
