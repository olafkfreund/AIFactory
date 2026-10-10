"""The GitHub token rides in a per-call GH_CONFIG_DIR, not the child env (#1688)."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "backend"))
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))

from core import child_env as ce  # noqa: E402
from core import git_credentials as gc  # noqa: E402

TOKEN = "ghp_1688FakeTokenSentinelDoNotLeak"  # gitleaks:allow
HELPER_KEY = "credential.https://github.com.helper"
HELPER_VALUE = "!gh auth git-credential"
_SERVER = _ROOT / "apps" / "web-server" / "server"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(k, raising=False)
    # Without this the sweep tests delete a running dev server's real dirs.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_token_rides_in_owner_only_hosts_file() -> None:
    with gc.github_env(ce.child_env()) as env:
        d = Path(env["GH_CONFIG_DIR"])
        assert d.name.startswith("aif-gh-")
        assert _mode(d) == 0o700
        assert _mode(d / "hosts.yml") == 0o600
        entry = json.loads((d / "hosts.yml").read_text())["github.com"]
        assert entry["oauth_token"] == TOKEN
        assert entry["user"] == "x-access-token"
        assert entry["git_protocol"] == "https"
        assert all(TOKEN not in v for v in env.values())
        for k in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_PASS"):
            assert k not in env


def test_inherited_github_credentials_are_stripped() -> None:
    base = {
        "PATH": "/usr/bin",
        "GITHUB_TOKEN": "old",
        "GH_TOKEN": "old2",
        "GIT_PASS": "p",
    }
    with gc.github_env(base) as env:
        for k in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_PASS"):
            assert k not in env


@pytest.mark.parametrize("how", ["ok", "error", "timeout"])
def test_dir_removed(how: str, tmp_path: Path) -> None:
    d = ""
    if how == "ok":
        with gc.github_env(ce.child_env()) as env:
            d = env["GH_CONFIG_DIR"]
    elif how == "error":
        with pytest.raises(RuntimeError), gc.github_env(ce.child_env()) as env:
            d = env["GH_CONFIG_DIR"]
            raise RuntimeError("boom")
    else:
        with (
            pytest.raises(subprocess.TimeoutExpired),
            gc.github_env(ce.child_env()) as env,
        ):
            d = env["GH_CONFIG_DIR"]
            subprocess.run(["sleep", "5"], timeout=0.1, env=env, check=False)  # noqa: S603, S607
    assert d
    assert not Path(d).exists()
    assert list(tmp_path.glob("aif-gh-*")) == []


def test_helper_entries_follow_hooks_path_and_existing_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "x")
    with gc.github_env(ce.child_env()) as env:
        assert env["GIT_CONFIG_KEY_0"] == "user.name"
        assert env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
        assert env["GIT_CONFIG_KEY_2"] == HELPER_KEY
        assert env["GIT_CONFIG_KEY_3"] == HELPER_KEY
        assert env["GIT_CONFIG_VALUE_2"] == ""
        assert env["GIT_CONFIG_VALUE_3"] == HELPER_VALUE
        assert env["GIT_CONFIG_COUNT"] == "4"


def test_malformed_config_count_does_not_raise() -> None:
    with gc.github_env({"PATH": "/usr/bin", "GIT_CONFIG_COUNT": "x"}) as env:
        assert env["GIT_CONFIG_KEY_0"] == HELPER_KEY
        assert env["GIT_CONFIG_KEY_1"] == HELPER_KEY
        assert env["GIT_CONFIG_COUNT"] == "2"


def test_token_in_extra_fails_closed(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError),
        gc.github_env(ce.child_env(extra={"AIF_PROBE": f"pre{TOKEN}"})),
    ):
        pass
    assert list(tmp_path.glob("aif-gh-*")) == []


def test_no_token_passes_base_env_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    base = ce.child_env()
    with gc.github_env(base) as env:
        assert env is base
        assert "GH_CONFIG_DIR" not in env
    assert list(tmp_path.glob("aif-gh-*")) == []


@pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("gh")), reason="needs git and gh"
)
def test_helper_reset_keeps_store_from_saving_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("[credential]\n\thelper = store\n")
    with gc.github_env(ce.child_env()) as env:
        env = {
            **env,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": str(gitconfig),
        }
        req = "protocol=https\nhost=github.com\n\n"
        fill = subprocess.run(  # noqa: S603
            ["git", "credential", "fill"],  # noqa: S607
            input=req,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert fill.returncode == 0
        assert TOKEN in fill.stdout
        approve = subprocess.run(  # noqa: S603
            ["git", "credential", "approve"],  # noqa: S607
            input=fill.stdout,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert approve.returncode == 0
    assert not (home / ".git-credentials").exists()


@pytest.mark.skipif(not shutil.which("gh"), reason="needs gh")
def test_real_gh_reads_token_without_rewriting_hosts() -> None:
    with gc.github_env(ce.child_env()) as env:
        d = Path(env["GH_CONFIG_DIR"])
        before = (d / "hosts.yml").read_bytes()
        r = subprocess.run(  # noqa: S603
            ["gh", "auth", "token"],  # noqa: S607
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert r.stdout.strip() == TOKEN
        assert sorted(p.name for p in d.iterdir()) == ["hosts.yml"]
        assert (d / "hosts.yml").read_bytes() == before


def test_sweep_removes_only_aif_gh_entries(tmp_path: Path) -> None:
    (tmp_path / "aif-gh-old").mkdir()
    (tmp_path / "aif-gh-old" / "hosts.yml").write_text("x")
    (tmp_path / "keep-me").mkdir()
    gc.sweep_github_dirs()
    assert not (tmp_path / "aif-gh-old").exists()
    assert (tmp_path / "keep-me").is_dir()


def test_sweep_does_not_follow_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "f").write_text("x")
    (tmp_path / "aif-gh-link").symlink_to(victim, target_is_directory=True)
    gc.sweep_github_dirs()
    assert (victim / "f").exists()


def test_askpass_reads_password_from_file() -> None:
    from server.services import project_workspace_service as pws

    secret = "workspace-secret-1688"  # gitleaks:allow
    with pws._git_askpass_env("oauth2", secret) as e:
        assert "GIT_PASS" not in e
        script = Path(e["GIT_ASKPASS"])
        pass_file = Path(e["GIT_PASS_FILE"])
        d = script.parent
        assert d.name.startswith("aif-gh-")
        assert pass_file.read_text() == secret
        assert _mode(pass_file) == 0o600
        assert _mode(script) == 0o700
        assert _mode(d) == 0o700
        run_env = {**ce.child_env(), **e}

        def ask(prompt: str) -> str:
            return subprocess.run(  # noqa: S603
                [str(script), prompt],
                capture_output=True,
                text=True,
                env=run_env,
                check=False,
            ).stdout

        assert ask("Password for 'https://h'") == secret
        assert ask("Username for 'https://h'") == "oauth2"
    assert not d.exists()


def test_web_server_has_no_env_token_path() -> None:
    needles = ("keep=GITHUB_KEEP", '"setup-git"', 'GIT_PASS"')
    hits = [
        f"{p.relative_to(_ROOT)}:{n}"
        for p in sorted(_SERVER.rglob("*.py"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if any(s in line for s in needles)
    ]
    assert hits == []


@pytest.mark.asyncio
async def test_login_route_spawns_nothing_when_token_set() -> None:
    from server.routes import github as github_routes

    spawn = AsyncMock(side_effect=AssertionError("must not spawn"))
    with (
        patch.object(github_routes.shutil, "which", return_value="/usr/bin/gh"),
        patch.object(github_routes.asyncio, "create_subprocess_exec", spawn),
    ):
        r = await github_routes.start_github_auth()
    assert r["data"]["success"] is True
    assert "GITHUB_TOKEN" in r["data"]["message"]
    spawn.assert_not_awaited()


def test_run_gh_command_fails_closed_when_tmp_unwritable() -> None:
    from server.services import gh

    def boom(*_a: object, **_k: object) -> str:
        raise PermissionError("tmp unwritable")

    def no_run(*_a: object, **_k: object) -> None:
        raise AssertionError("gh must not run without a config dir")

    with (
        patch.object(tempfile, "mkdtemp", boom),
        patch.object(gh.subprocess, "run", no_run),
    ):
        assert gh.run_gh_command(["pr", "list"])["success"] is False
