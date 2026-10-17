"""Private agent HOME copies only the selected CLI's login files."""

from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "backend"))

from core.agent_home import AgentHome  # noqa: E402


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setenv("HOME", str(real))
    for name in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "GIT_CONFIG_GLOBAL",
    ):
        monkeypatch.delenv(name, raising=False)
    return real


def login(real: Path, value: bytes = b'{"token":"old"}') -> Path:
    path = real / ".codex" / "auth.json"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(value)
    return path


def test_only_selected_login_is_copied(fake_home: Path) -> None:
    login(fake_home)
    for path in (
        ".claude/.credentials.json",
        ".gemini/oauth_creds.json",
        ".ssh/id_ed25519",
    ):
        other = fake_home / path
        other.parent.mkdir(exist_ok=True)
        other.write_text("private")
    with AgentHome("codex") as h:
        home = Path(h.env["HOME"])
        copied = home / ".codex" / "auth.json"
        assert copied.read_bytes() == b'{"token":"old"}'
        assert copied.stat().st_mode & 0o777 == 0o600
        assert copied.parent.stat().st_mode & 0o777 == 0o700
        assert home.stat().st_mode & 0o777 == 0o700
        assert Path(h.env["TMPDIR"]).stat().st_mode & 0o777 == 0o700
        assert h.env["CODEX_HOME"] == str(copied.parent)
        assert not any(
            (home / path).exists()
            for path in (
                ".claude/.credentials.json",
                ".gemini/oauth_creds.json",
                ".ssh/id_ed25519",
            )
        )


def test_valid_change_writes_back(fake_home: Path) -> None:
    real = login(fake_home)
    with AgentHome("codex") as h:
        (Path(h.env["CODEX_HOME"]) / "auth.json").write_bytes(b'{"token":"new"}')
    assert real.read_bytes() == b'{"token":"new"}'
    assert real.stat().st_mode & 0o777 == 0o600


def test_unchanged_login_is_not_touched(fake_home: Path) -> None:
    real = login(fake_home)
    before = real.stat().st_mtime_ns
    with AgentHome("codex"):
        pass
    assert real.stat().st_mtime_ns == before


def test_invalid_json_is_not_written_back(fake_home: Path) -> None:
    real = login(fake_home)
    with AgentHome("codex") as h:
        (Path(h.env["CODEX_HOME"]) / "auth.json").write_text("broken")
    assert real.read_bytes() == b'{"token":"old"}'


def test_concurrent_refresh_is_kept(fake_home: Path) -> None:
    real = login(fake_home)
    with AgentHome("codex") as h:
        (Path(h.env["CODEX_HOME"]) / "auth.json").write_bytes(b'{"token":"child"}')
        real.write_bytes(b'{"token":"other"}')
    assert real.read_bytes() == b'{"token":"other"}'


def test_settings_changes_are_not_written_back(fake_home: Path) -> None:
    path = fake_home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"fastMode":false}')
    with AgentHome("claude") as h:
        (Path(h.env["CLAUDE_CONFIG_DIR"]) / "settings.json").write_text(
            '{"fastMode":true}'
        )
    assert path.read_text() == '{"fastMode":false}'


def test_close_and_finalizer_remove_home(fake_home: Path) -> None:
    h = AgentHome("bash")
    home = Path(h.env["HOME"])
    h.close()
    h.close()
    assert not home.exists()
    h = AgentHome("bash")
    home = Path(h.env["HOME"])
    del h
    gc.collect()
    assert not home.exists()


def test_creation_failure_does_not_fall_back(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**_kwargs: object) -> None:
        raise OSError("cannot create")

    monkeypatch.setattr("core.agent_home.tempfile.mkdtemp", fail)
    with pytest.raises(OSError, match="cannot create"):
        AgentHome("codex")


def test_copy_failure_removes_private_home(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read 000 files")
    real = login(fake_home)
    real.chmod(0)
    monkeypatch.setattr("core.agent_home.tempfile.tempdir", str(tmp_path))
    with pytest.raises(PermissionError):
        AgentHome("codex")
    assert not list(tmp_path.glob("aif-home-*"))


def test_codex_home_override(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override"
    override.mkdir()
    (override / "auth.json").write_text('{"override":true}')
    monkeypatch.setenv("CODEX_HOME", str(override))
    with AgentHome("codex") as h:
        assert (
            Path(h.env["CODEX_HOME"]) / "auth.json"
        ).read_text() == '{"override":true}'


def test_gitconfig_copies_only_identity(fake_home: Path) -> None:
    (fake_home / ".gitconfig").write_text(
        '[user]\n\tname = "Alice"\n[credential]\n\thelper = store\n'
        "[safe]\n\tdirectory = /work\n"
    )
    with AgentHome("bash") as h:
        config = (Path(h.env["HOME"]) / ".gitconfig").read_text()
    assert '[user]\n\tname = "Alice"' in config
    assert '[safe]\n\tdirectory = "/work"' in config
    assert "credential" not in config


def test_unknown_cli_rejected(fake_home: Path) -> None:
    with pytest.raises(ValueError):
        AgentHome("unknown")
