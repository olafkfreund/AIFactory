# ruff: noqa: ARG001, S105, S603, S607, PLW1510
"""Child processes must not inherit the web server's secrets (#1680)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "backend"))
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))

from server.utils.subprocess_env import (  # noqa: E402
    AGENT_GITHUB_TOKEN_VAR,
    GITHUB_KEEP,
    child_env,
    dotenv_key_blocked,
    make_subprocess_env,
)

SECRETS = (
    "DATABASE_URL",
    "JWT_SECRET",
    "API_TOKEN",
    "AIFACTORY_TOKEN",
    "AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY",
    "S3_SECRET_KEY",
    "APP_OIDC_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "KMS_FERNET_KEY",
)


@pytest.fixture
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_PAGER",
    ):
        monkeypatch.delenv(k, raising=False)
    for k in SECRETS:
        monkeypatch.setenv(k, "s3cret")
    monkeypatch.setenv("OPENAI_API_KEY", "oa")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "oc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    monkeypatch.setenv("HOME", os.environ.get("HOME", "/root"))


def test_child_env_drops_secrets(secret_env: None) -> None:
    env = child_env()
    for k in SECRETS:
        assert k not in env, f"leaked {k}"


def test_strip_vars_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("anthropic_api_key_file", "x")
    assert "anthropic_api_key_file" not in child_env()
    assert "anthropic_api_key_file" not in child_env(runner=True)


def test_child_env_keeps_path_and_home(secret_env: None) -> None:
    env = child_env()
    assert env["PATH"] == os.environ["PATH"]
    assert env["HOME"] == os.environ["HOME"]


def test_agent_child_env_allowlist(
    secret_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from core.child_env import child_env as core_child_env

    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("FOO_SECRETISH", "FOO_MISC", "SSH_AUTH_SOCK", "MY_TOOL_DIR"):
        monkeypatch.setenv(name, "x")

    agent_env = core_child_env(
        agent=True,
        keep=("MY_TOOL_DIR",),
        extra={"AGENT_EXTRA": "set"},
    )
    assert "FOO_SECRETISH" not in agent_env
    assert "FOO_MISC" not in agent_env
    assert "SSH_AUTH_SOCK" not in agent_env
    assert agent_env["PATH"] == os.environ["PATH"]
    assert agent_env["MY_TOOL_DIR"] == "x"
    assert agent_env["AGENT_EXTRA"] == "set"
    assert agent_env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert agent_env["GIT_CONFIG_VALUE_0"] == "/dev/null"

    default_env = core_child_env()
    assert "FOO_SECRETISH" not in default_env  # Existing deny pattern matches SECRET.
    assert default_env["FOO_MISC"] == "x"
    assert default_env["SSH_AUTH_SOCK"] == "x"


def test_keep_restores_only_named(secret_env: None) -> None:
    env = child_env(keep=GITHUB_KEEP)
    assert env["GITHUB_TOKEN"] == "s3cret"
    assert "DATABASE_URL" not in env


def test_hooks_path_appended_after_existing_entry(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.https://github.com.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!gh auth git-credential")
    env = child_env()
    assert env["GIT_CONFIG_COUNT"] == "11"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_0"] == "!gh auth git-credential"
    assert env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_1"] == "/dev/null"
    assert env["GIT_CONFIG_KEY_2"] == "core.fsmonitor"
    assert env["GIT_CONFIG_VALUE_2"] == "false"
    assert env["GIT_CONFIG_KEY_3"] == "protocol.ext.allow"
    assert env["GIT_CONFIG_VALUE_3"] == "never"
    assert env["GIT_CONFIG_KEY_4"] == "core.sshCommand"
    assert env["GIT_CONFIG_VALUE_4"] == "ssh"
    assert env["GIT_CONFIG_KEY_5"] == "core.pager"
    assert env["GIT_CONFIG_VALUE_5"] == "cat"
    assert env["GIT_CONFIG_KEY_6"] == "core.editor"
    assert env["GIT_CONFIG_VALUE_6"] == "true"
    assert env["GIT_CONFIG_KEY_7"] == "core.askPass"
    assert env["GIT_CONFIG_VALUE_7"] == ""
    assert env["GIT_CONFIG_KEY_8"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_8"] == ""
    assert env["GIT_CONFIG_KEY_9"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_9"] == "!gh auth git-credential"
    assert env["GIT_CONFIG_KEY_10"] == "sequence.editor"
    assert env["GIT_CONFIG_VALUE_10"] == "true"


def test_hooks_path_is_entry_zero_without_count(secret_env: None) -> None:
    env = child_env()
    assert env["GIT_CONFIG_COUNT"] == "10"
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_0"] == "/dev/null"
    assert env["GIT_CONFIG_KEY_1"] == "core.fsmonitor"
    assert env["GIT_CONFIG_VALUE_1"] == "false"
    assert env["GIT_CONFIG_KEY_2"] == "protocol.ext.allow"
    assert env["GIT_CONFIG_VALUE_2"] == "never"
    assert env["GIT_CONFIG_KEY_3"] == "core.sshCommand"
    assert env["GIT_CONFIG_VALUE_3"] == "ssh"
    assert env["GIT_CONFIG_KEY_4"] == "core.pager"
    assert env["GIT_CONFIG_VALUE_4"] == "cat"
    assert env["GIT_CONFIG_KEY_5"] == "core.editor"
    assert env["GIT_CONFIG_VALUE_5"] == "true"
    assert env["GIT_CONFIG_KEY_6"] == "core.askPass"
    assert env["GIT_CONFIG_VALUE_6"] == ""
    assert env["GIT_CONFIG_KEY_7"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_7"] == ""
    assert env["GIT_CONFIG_KEY_8"] == "credential.https://github.com.helper"
    assert env["GIT_CONFIG_VALUE_8"] == "!gh auth git-credential"
    assert env["GIT_CONFIG_KEY_9"] == "sequence.editor"
    assert env["GIT_CONFIG_VALUE_9"] == "true"


def test_real_git_fsmonitor_disabled_by_child_env(
    secret_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pre-commit runs pytest under a git hook: ambient GIT_* would aim git at this repo (#819)
    for k in (
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    ):
        monkeypatch.delenv(k, raising=False)
    iso = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    base = {**os.environ, **iso}
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=base)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        ],
        check=True,
        env=base,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "-b", "w"],
        check=True,
        env=base,
    )
    subprocess.run(
        ["git", "-C", str(wt), "config", "core.fsmonitor", "touch M; false"],
        check=True,
        env=base,
    )
    subprocess.run(
        ["git", "-C", str(repo), "status"],
        env={**child_env(), **iso},
        capture_output=True,
    )
    assert not (repo / "M").exists()
    subprocess.run(
        ["git", "-C", str(repo), "status"], env=base, capture_output=True
    )  # control
    assert (repo / "M").exists()


def test_make_subprocess_env_keep_and_drop(secret_env: None) -> None:
    env = make_subprocess_env()
    assert env["OPENAI_API_KEY"] == "oa"
    assert env["OPENAI_COMPATIBLE_API_KEY"] == "oc"
    assert "GITHUB_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_make_subprocess_env_can_keep_anthropic_key(secret_env: None) -> None:
    env = make_subprocess_env(strip_anthropic_api_key=False)
    assert env["ANTHROPIC_API_KEY"] == "an"


def test_real_git_hook_disabled_by_child_env(secret_env: None, tmp_path: Path) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    base = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=base)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    cmd = [*git, "-C", str(tmp_path), "commit", "--allow-empty", "-qm", "x"]
    assert subprocess.run(cmd, env=base, capture_output=True).returncode != 0
    scrubbed = {
        **child_env(),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    assert subprocess.run(cmd, env=scrubbed, capture_output=True).returncode == 0


def test_real_git_ssh_pager_editor_pinned(
    secret_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent-written .git/config cannot pick ssh, pager or editor (#1717)."""
    from core import workspace_fetch

    for k in (
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "GIT_PAGER",
        "PAGER",
        "GIT_EDITOR",
        "VISUAL",
        "EDITOR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("WORKSPACE_URI", "s3://dummy/ws.tar")
    spec = "001-pin"
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / spec
    marker = tmp_path / "S"
    pager = f"touch {tmp_path}/P; cat"
    editor = f"touch {tmp_path}/E; true"

    def git(*args: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(wt), *args],
            env=env or {**os.environ},
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(wt)],
        env={**os.environ},
        check=True,
        timeout=30,
    )
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    git("remote", "add", "origin", "ssh://git@127.0.0.1:1/r.git")
    git("config", "ssh.variant", "ssh")  # 'simple' variant rejects the port
    git("config", "core.sshCommand", f"touch {marker}; false")
    git("config", "core.pager", pager)
    git("config", "core.editor", editor)
    git("push", "origin", "HEAD:main")  # control: unpinned git runs it
    assert marker.exists()
    marker.unlink()
    assert workspace_fetch.maybe_push_workspace_branch(tmp_path, spec) is False
    assert not marker.exists()
    pinned = child_env()
    assert git("var", "GIT_PAGER") == pager
    assert git("var", "GIT_EDITOR") == editor
    assert git("var", "GIT_PAGER", env=pinned) == "cat"
    assert git("var", "GIT_EDITOR", env=pinned) == "true"
    monkeypatch.setenv("GIT_PAGER", "less")
    monkeypatch.setenv("GIT_EDITOR", "vi")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i k")
    op = child_env()
    assert op["GIT_SSH_COMMAND"] == "ssh -i k"
    assert git("var", "GIT_PAGER", env=op) == "less"
    assert git("var", "GIT_EDITOR", env=op) == "vi"


def test_real_git_repo_exec_keys_pinned(secret_env: None, tmp_path: Path) -> None:
    """An agent-written .git/config cannot pick askPass, credential.helper, or sequence.editor (#1745)."""
    iso = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    base = {
        k: v
        for k, v in {**os.environ, **iso}.items()
        if k
        not in (
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
        )
    }
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=base)
    for key, val in (
        ("core.askPass", f"{tmp_path}/A"),
        ("credential.helper", f"{tmp_path}/C"),
        ("sequence.editor", f"{tmp_path}/Q"),
    ):
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", key, val],
            check=True,
            env=base,
        )
    env = child_env()
    run_env = {
        k: v
        for k, v in {**env, **iso}.items()
        if k
        not in (
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
        )
    }

    def cfg(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "config", *args],
            check=True,
            capture_output=True,
            text=True,
            env=run_env,
        ).stdout

    assert cfg("--get", "core.askPass").strip() == ""
    assert cfg("--get-all", "credential.helper").splitlines()[-1] == ""
    assert (
        cfg("--get-all", "credential.https://github.com.helper").splitlines()[-1]
        == "!gh auth git-credential"
    )
    assert cfg("--get", "sequence.editor").strip() == "true"
    assert env["GIT_PAGER"] == "cat"  # beats pager.<cmd> on a tty


def _credential_fill(tmp_path: Path, env: dict[str, str]) -> None:
    """Run ``git credential fill`` for a fake host with no gh on PATH, no network."""
    import shutil

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("git", "touch"):
        found = shutil.which(tool)
        assert found
        (bin_dir / tool).symlink_to(found)
    run_env = {
        **env,
        "PATH": str(bin_dir),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for k in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_DIR", "GIT_WORK_TREE"):
        run_env.pop(k, None)
    subprocess.run(
        ["git", "-C", str(tmp_path / "repo"), "credential", "fill"],
        input="protocol=https\nhost=example.invalid\n\n",
        capture_output=True,
        text=True,
        env=run_env,
        timeout=30,
    )


def test_real_git_planted_helper_and_askpass_do_not_run(
    secret_env: None, tmp_path: Path
) -> None:
    """A repo-planted credential.helper and core.askPass never run (#1745)."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ask = tmp_path / "ask.sh"
    ask.write_text(f"#!/bin/sh\ntouch {tmp_path}/asked\n")
    ask.chmod(0o700)
    for key, val in (
        ("credential.helper", f"!touch {tmp_path}/ran"),
        ("core.askPass", str(ask)),
    ):
        subprocess.run(["git", "-C", str(repo), "config", key, val], check=True)
    _credential_fill(tmp_path, child_env())
    assert not (tmp_path / "ran").exists()
    assert not (tmp_path / "asked").exists()


def test_reset_wipes_a_callers_earlier_credential_helper(
    secret_env: None, tmp_path: Path
) -> None:
    """A helper passed in ``extra`` before the pins is reset by them (#1745)."""
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    env = child_env(
        extra={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": f"!touch {tmp_path}/caller",
        }
    )
    _credential_fill(tmp_path, env)
    assert not (tmp_path / "caller").exists()


# --- backend in-process helpers (#1680 review fix) ---------------------------


@pytest.mark.parametrize(
    "url", ["https://github.com/o/r.git", "https://example.com/o/r.git"]
)
# ids: PYTEST_CURRENT_TEST (kept in child env) must not quote the token
@pytest.mark.parametrize("token", ["ghs_scrubtest", ""], ids=["token", "none"])
def test_authed_push_url_env_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, url: str, token: str
) -> None:
    from core.git_credentials import authed_push_url

    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    with authed_push_url(url) as (_, env):
        assert "DATABASE_URL" not in env
        assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert env["GIT_CONFIG_VALUE_0"] == "/dev/null"
        # the token only travels via a per-call GH_CONFIG_DIR (#1723)
        assert "GITHUB_TOKEN" not in env
        assert "GIT_PASS" not in env
        assert ("GH_CONFIG_DIR" in env) == bool(
            token and url.startswith("https://github.com/")
        )


def test_core_child_env_extra_count_appends_hooks_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.child_env import child_env as core_child_env

    env = core_child_env(
        extra={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "x",
        }
    )
    assert env["GIT_CONFIG_COUNT"] == "11"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env["GIT_CONFIG_KEY_2"] == "core.fsmonitor"
    assert env["GIT_CONFIG_KEY_3"] == "protocol.ext.allow"
    assert env["GIT_CONFIG_KEY_4"] == "core.sshCommand"
    assert env["GIT_CONFIG_KEY_5"] == "core.pager"
    assert env["GIT_CONFIG_KEY_6"] == "core.editor"
    assert env["GIT_CONFIG_VALUE_1"] == "/dev/null"


def test_opt_in_keeps_both_anthropic_credentials(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", "/run/key")
    env = make_subprocess_env(strip_anthropic_api_key=False)
    assert env["ANTHROPIC_API_KEY_FILE"] == "/run/key"
    assert "ANTHROPIC_API_KEY_FILE" not in make_subprocess_env()


def test_trusted_plan_git_env_is_scrubbed_and_headless(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trusted_plan  # noqa: PLC0415

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    env = trusted_plan._git_subprocess_env()
    assert env is not None
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_1"] == "core.fsmonitor"
    assert "DATABASE_URL" not in env
    assert env["GIT_CONFIG_VALUE_0"] == "/dev/null"


def test_credential_names_dropped_for_tools_kept_for_runner(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CONTEXT7_KEY",
        "S3_ACCESS_KEY",
        "OLLAMA_API_KEY",
    ):
        monkeypatch.setenv(k, "c")
    tools = child_env()
    for k in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CONTEXT7_KEY",
        "S3_ACCESS_KEY",
        "OLLAMA_API_KEY",
    ):
        assert k not in tools, f"leaked {k}"
    assert (
        child_env(keep=("CLAUDE_CODE_OAUTH_TOKEN",))["CLAUDE_CODE_OAUTH_TOKEN"] == "c"
    )
    runner = make_subprocess_env()
    assert runner["CLAUDE_CODE_OAUTH_TOKEN"] == "c"
    # denied, but RUNNER_KEEP restores it for the runner (#1713)
    assert runner["OLLAMA_API_KEY"] == "c"
    assert "CONTEXT7_KEY" not in runner  # #1674
    assert "S3_ACCESS_KEY" not in runner  # #1674
    assert "DATABASE_URL" not in runner


def test_github_app_private_key_never_reaches_a_child(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The App key is popped at boot; the deny pattern is the second layer (#1671)."""
    name = "AIFACTORY_GITHUB_APP_PRIVATE_KEY"
    monkeypatch.setenv(name, "-----BEGIN RSA PRIVATE KEY-----x")
    assert name not in child_env()
    assert name not in child_env(keep=GITHUB_KEEP)
    assert name not in make_subprocess_env()
    assert name not in make_subprocess_env(strip_anthropic_api_key=False)


def test_make_subprocess_env_drops_trusted_contract(
    secret_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIFACTORY_TRUSTED_CONTRACT", "x")
    assert "AIFACTORY_TRUSTED_CONTRACT" not in make_subprocess_env()
    assert "AIFACTORY_TRUSTED_CONTRACT" not in child_env()


def test_extra_trusted_contract_survives_strip(secret_env: None) -> None:
    env = make_subprocess_env(extra={"AIFACTORY_TRUSTED_CONTRACT": "d"})
    assert env["AIFACTORY_TRUSTED_CONTRACT"] == "d"


@pytest.fixture
def github_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import mcp_credentials  # noqa: PLC0415

    monkeypatch.setattr(
        mcp_credentials,
        "_load_operator_config",
        lambda: {"github": {"tokenEnv": "GITHUB_MCP_TOKEN"}},
    )
    for name in (
        *GITHUB_KEEP,
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GITHUB_MCP_TOKEN",
        AGENT_GITHUB_TOKEN_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in zip(
        (*GITHUB_KEEP, "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_MCP_TOKEN"),
        ("cp-a", "cp-b", "cp-c", "cp-d"),
        strict=True,
    ):
        monkeypatch.setenv(name, value)


def test_github_env_is_scoped_to_selected_runner(
    github_tokens: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = (*GITHUB_KEEP, "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_MCP_TOKEN")
    assert all(name not in make_subprocess_env() for name in names)
    assert {name: make_subprocess_env(github=True)[name] for name in names} == dict(
        zip(names, ("cp-a", "cp-b", "cp-c", "cp-d"), strict=True)
    )
    monkeypatch.setenv(AGENT_GITHUB_TOKEN_VAR, "agent-x")
    plain = make_subprocess_env()
    assert all(name not in plain for name in (*names, AGENT_GITHUB_TOKEN_VAR))
    assert "agent-x" not in plain.values()
    env = make_subprocess_env(github=True)
    assert all(env[name] == "agent-x" for name in names)
    assert AGENT_GITHUB_TOKEN_VAR not in env
    assert not any(value in env.values() for value in ("cp-a", "cp-b", "cp-c", "cp-d"))
    assert AGENT_GITHUB_TOKEN_VAR not in child_env()
    from core.child_env import child_env as core_child_env  # noqa: PLC0415

    assert AGENT_GITHUB_TOKEN_VAR not in core_child_env(agent=True)
    from core.auth import is_denied_env_key  # noqa: PLC0415

    assert is_denied_env_key(AGENT_GITHUB_TOKEN_VAR)


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_agent_token_warns_once_and_falls_back(
    github_tokens: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    blank: str,
) -> None:
    from server.utils import subprocess_env  # noqa: PLC0415

    expected = make_subprocess_env(github=True)
    monkeypatch.setattr(subprocess_env, "_blank_agent_token_warned", False)
    monkeypatch.setenv(AGENT_GITHUB_TOKEN_VAR, blank)
    with caplog.at_level("WARNING"):
        assert make_subprocess_env(github=True) == expected
        assert make_subprocess_env(github=True) == expected
    assert len(caplog.records) == 1
    assert "control plane's GitHub token" in caplog.text
    monkeypatch.delenv(AGENT_GITHUB_TOKEN_VAR)
    caplog.clear()
    assert make_subprocess_env(github=True) == expected
    assert not caplog.records


def test_dotenv_guard_and_loaders(
    github_tokens: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from server.services.agent_service import _load_backend_env  # noqa: PLC0415
    from server.services.agent_spec_creation import _load_spec_env  # noqa: PLC0415
    from server.services.pr_review_service import _load_env_file  # noqa: PLC0415

    file = tmp_path / ".env"
    file.write_text(
        "GITHUB_TOKEN=env-1\nGITHUB_PERSONAL_ACCESS_TOKEN=env-2\n"
        "GITHUB_MCP_TOKEN=env-3\nAIFACTORY_AGENT_GITHUB_TOKEN=env-4\nFOO=bar\n"
    )
    for load in (_load_backend_env, _load_env_file, _load_spec_env):
        monkeypatch.setenv(AGENT_GITHUB_TOKEN_VAR, "agent-x")
        assert dotenv_key_blocked(AGENT_GITHUB_TOKEN_VAR)
        assert all(
            dotenv_key_blocked(k)
            for k in (
                "GITHUB_TOKEN",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "GITHUB_MCP_TOKEN",
            )
        )
        assert not dotenv_key_blocked("OPENAI_API_KEY")
        env = make_subprocess_env(github=True)
        load(file, env)
        assert env["FOO"] == "bar"
        assert not any(
            value in env.values() for value in ("env-1", "env-2", "env-3", "env-4")
        )
        monkeypatch.delenv(AGENT_GITHUB_TOKEN_VAR)
        assert not any(
            dotenv_key_blocked(name)
            for name in (
                "GITHUB_TOKEN",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "GITHUB_MCP_TOKEN",
            )
        )
        env = make_subprocess_env(github=True)
        env.pop("GITHUB_MCP_TOKEN")
        load(file, env)
        assert env["GITHUB_MCP_TOKEN"] == "env-3"
        assert AGENT_GITHUB_TOKEN_VAR not in env


def test_subprocess_env_callers_are_explicit() -> None:
    server = _ROOT / "apps" / "web-server" / "server"
    calls = {}
    for path in server.rglob("*.py"):
        found = [
            node
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_subprocess_env"
        ]
        if found:
            calls[path.name] = [
                [
                    (kw.arg, ast.literal_eval(kw.value))
                    for kw in node.keywords
                    if kw.arg == "github"
                ]
                for node in found
            ]
    assert calls == {
        "agent_service.py": [[("github", True)]],
        "agent_spec_creation.py": [[("github", True)]],
        "pr_review_service.py": [[("github", True)]],
        "changelog_service.py": [[]],
        "clarification_service.py": [[]],
        "insights_service.py": [[]],
    }
