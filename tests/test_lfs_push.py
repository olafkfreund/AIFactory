"""Explicit, locked-down ``git lfs push`` before server PR pushes (#1690).

#1680 turned git hooks off (``core.hooksPath=/dev/null``), so git-lfs's
``pre-push`` hook no longer uploads LFS objects. ``lfs_push_argv`` builds the
pinned upload command and ``push_with_lfs`` runs it before the ref push.
"""

from __future__ import annotations

import logging
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from core import child_env  # noqa: E402
from server.services import pr_endgame  # noqa: E402

CmdResult = pr_endgame.CmdResult
E = "https://github.com/o/r.git/info/lfs"
_REAL_WHICH = shutil.which
_HAS_LFS = _REAL_WHICH("git-lfs") is not None


def _attr(mod: ModuleType, name: str) -> Any:
    """Fetch a not-yet-written name, so the red state is an AttributeError."""
    return getattr(mod, name)


def _argv(output: str, ref: str = "HEAD") -> list[str] | None:
    fn: Callable[[str, str], list[str] | None] = _attr(child_env, "lfs_push_argv")
    return fn(output, ref)


def _push_with_lfs(
    push_argv: list[str], ref: str, cwd: str, runner: Callable[..., Any]
) -> Any:
    return _attr(pr_endgame, "push_with_lfs")(push_argv, ref, cwd, runner)


def _which_lfs(present: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(cmd: str, *a: Any, **kw: Any) -> str | None:
        if cmd == "git-lfs":
            return "/x/git-lfs" if present else None
        return _REAL_WHICH(cmd, *a, **kw)

    monkeypatch.setattr("core.child_env.shutil.which", _which)


def _pinned(ref: str = "HEAD", e: str = E) -> list[str]:
    return [
        "git",
        "-c",
        f"lfs.url={e}",
        "-c",
        f"lfs.pushurl={e}",
        "-c",
        f"lfs.{e}.locksverify=false",
        "-c",
        "lfs.allowincompletepush=false",
        "lfs",
        "push",
        "origin",
        ref,
    ]


# ── helper ──────────────────────────────────────────────────────────────────


def test_helper_none_when_git_lfs_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(False, monkeypatch)
    assert _argv("https://github.com/o/r") is None


_REJECTS = [
    "git@github.com:o/r.git",
    "ssh://git@github.com/o/r",
    "http://github.com/o/r",
    "https://gitlab.com/o/r",
    "https://github.com.evil.com/o/r",
    "https://x-access-token:SECRET@github.com/o/r",
    "https://github.com/o/r?x=1",
    "https://github.com/o/r#x",
    "https://github.com/o/..",
    "https://github.com/o/.",
    "https://github.com/o/..git",
    "https://github.com/../r",
    "https://github.com/o/r/x",
    "https://github.com/o/r\nhttps://github.com/o/s",
    "",
]


@pytest.mark.parametrize("url", _REJECTS)
def test_helper_rejects(
    url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _which_lfs(True, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="core.child_env"):
        assert _argv(url) is None
    assert any(
        r.name == "core.child_env" and r.levelno == logging.WARNING
        for r in caplog.records
    )
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r",
        "https://github.com/o/r.git",
        "https://github.com/o/r.git/",
    ],
)
def test_helper_exact_argv(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(True, monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECRET")
    argv = _argv(url)
    assert argv == _pinned()
    assert argv is not None
    joined = " ".join(argv)
    assert "ghp_SECRET" not in joined
    assert "@" not in joined


def test_helper_accepts_real_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(True, monkeypatch)
    e = "https://github.com/my-org/my.repo_1.git/info/lfs"
    assert _argv("https://github.com/my-org/my.repo_1") == _pinned(e=e)


# ── wrapper ─────────────────────────────────────────────────────────────────

_GITHUB = "https://github.com/o/r"
_PUSH = ["git", "push", "origin", "HEAD"]


class Rec:
    """Recording runner; ``results`` overrides per call kind."""

    def __init__(self, **results: Any) -> None:
        self.results = results
        self.calls: list[list[str]] = []
        self.cwds: list[str | None] = []

    @staticmethod
    def kind(argv: list[str]) -> str:
        if argv[:3] == ["git", "remote", "get-url"]:
            return "geturl"
        if "--get-regexp" in argv:
            return "check"
        if "lfs" in argv and argv[argv.index("lfs") + 1] == "push":
            return "upload"
        if argv[:2] == ["git", "push"]:
            return "push"
        return "other"

    def __call__(self, argv: list[str], cwd: str | None = None) -> Any:
        self.calls.append(argv)
        self.cwds.append(cwd)
        default = {
            "geturl": CmdResult(0, _GITHUB, ""),
            "check": CmdResult(1, "", ""),
            "upload": CmdResult(0, "", ""),
            "push": CmdResult(0, "", ""),
        }
        kind = self.kind(argv)
        return self.results.get(kind, default.get(kind))

    def kinds(self) -> list[str]:
        return [self.kind(c) for c in self.calls]


def test_upload_failure_skips_push(monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(True, monkeypatch)
    boom = CmdResult(2, "", "boom")
    rec = Rec(upload=boom)
    out = _push_with_lfs(_PUSH, "HEAD", "/wt", rec)
    assert out is boom
    assert "push" not in rec.kinds()


@pytest.mark.parametrize("rc", [0, 128], ids=["defined", "check-error"])
def test_customtransfer_refuses(rc: int, monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(True, monkeypatch)
    rec = Rec(check=CmdResult(rc, "lfs.customtransfer.x.path" if rc == 0 else "", ""))
    out = _push_with_lfs(_PUSH, "HEAD", "/wt", rec)
    assert not out.ok
    assert rec.kinds() == ["geturl", "check"]


def test_all_ok_order_and_push_argv_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(True, monkeypatch)
    pushed = CmdResult(0, "pushed", "")
    rec = Rec(push=pushed)
    out = _push_with_lfs(_PUSH, "HEAD", "/wt", rec)
    assert rec.kinds() == ["geturl", "check", "upload", "push"]
    assert rec.calls[-1] == _PUSH
    assert set(rec.cwds) == {"/wt"}
    assert out is pushed


def test_get_url_failure_pushes_without_lfs(monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(True, monkeypatch)
    rec = Rec(geturl=CmdResult(2, _GITHUB, "err"))
    _push_with_lfs(_PUSH, "HEAD", "/wt", rec)
    assert rec.kinds() == ["geturl", "push"]


def test_non_github_remote_pushes_without_lfs(monkeypatch: pytest.MonkeyPatch) -> None:
    _which_lfs(True, monkeypatch)
    rec = Rec(geturl=CmdResult(0, "/srv/origin.git", ""))
    _push_with_lfs(_PUSH, "HEAD", "/wt", rec)
    assert rec.kinds() == ["geturl", "push"]


# ── integration (real git + git-lfs, no network) ────────────────────────────

_needs_lfs = pytest.mark.skipif(not _HAS_LFS, reason="git-lfs not installed")
_ID = [
    "-c",
    "user.email=t@example.com",
    "-c",
    "user.name=T",
    "-c",
    "commit.gpgsign=false",
]


def _g(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", *_ID, *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return p.stdout.strip()


@pytest.fixture()
def lfs_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    for k in ("HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    for k in (
        "NO_PROXY",
        "no_proxy",
        "https_proxy",
        "http_proxy",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    _which_lfs(True, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "--initial-branch", "main")
    _g(repo, "remote", "add", "origin", _GITHUB)
    (repo / ".lfsconfig").write_text("[lfs]\n\turl = https://evil.invalid/a\n")
    (repo / "f.txt").write_text("x\n")
    _g(repo, "add", ".")
    _g(repo, "commit", "-m", "seed")
    _g(repo, "config", "lfs.url", "https://evil.invalid/c")
    _g(repo, "config", "lfs.pushurl", "https://evil.invalid/b")
    _g(repo, "config", "lfs.allowincompletepush", "true")
    _g(repo, "config", "lfs.locksverify", "true")
    return repo


def _pinned_argv(repo: Path) -> list[str]:
    argv = _argv(_g(repo, "remote", "get-url", "--push", "--all", "origin"))
    assert argv is not None
    return argv


@_needs_lfs
def test_pins_override_lfsconfig_and_git_config(lfs_repo: Path) -> None:
    argv = _pinned_argv(lfs_repo)
    base = argv[: argv.index("lfs")]
    run = pr_endgame._default_runner
    env = run([*base, "lfs", "env"], str(lfs_repo))
    assert f"Endpoint={E} (" in env.out
    for key, want in (
        ("lfs.pushurl", E),
        ("lfs.allowincompletepush", "false"),
        (f"lfs.{E}.locksverify", "false"),
    ):
        assert run([*base, "config", "--get", key], str(lfs_repo)).out == want


@_needs_lfs
def test_no_object_push_is_offline_and_credential_free(lfs_repo: Path) -> None:
    # The proxy is dead: any network call would make this rc 2.
    assert pr_endgame._default_runner(_pinned_argv(lfs_repo), str(lfs_repo)).rc == 0


def _evil_setup(repo: Path) -> Path:
    _g(repo, "lfs", "install", "--local")
    _g(repo, "lfs", "track", "*.bin")
    (repo / "a.bin").write_bytes(b"x" * 100)
    _g(repo, "add", ".")
    _g(repo, "commit", "-m", "lfs")
    assert _g(repo, "lfs", "ls-files")
    pwned = repo.parent / "PWNED"
    agent = repo.parent / "agent.sh"
    agent.write_text(f"#!/bin/sh\ntouch {pwned}\nexit 1\n")
    agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
    _g(repo, "config", "lfs.customtransfer.evil.path", str(agent))
    _g(repo, "config", f"lfs.{E}.standalonetransferagent", "evil")
    return pwned


@_needs_lfs
def test_evil_standalone_agent_is_refused(lfs_repo: Path) -> None:
    pwned = _evil_setup(lfs_repo)
    seen: list[list[str]] = []

    def rec(argv: list[str], cwd: str | None = None) -> Any:
        seen.append(argv)
        if Rec.kind(argv) == "push":
            return CmdResult(0, "", "")
        return pr_endgame._default_runner(argv, cwd)

    out = _push_with_lfs(_PUSH, "HEAD", str(lfs_repo), rec)
    assert not out.ok
    assert not pwned.exists()
    kinds = [Rec.kind(a) for a in seen]
    assert "upload" not in kinds
    assert "push" not in kinds


@_needs_lfs
def test_pins_alone_do_not_stop_the_agent(lfs_repo: Path) -> None:
    # Positive control: without the D7 guard the agent runs, so I3 is load-bearing.
    pwned = _evil_setup(lfs_repo)
    pr_endgame._default_runner(_pinned_argv(lfs_repo), str(lfs_repo))
    assert pwned.exists()


@_needs_lfs
@pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
def test_url_rewrite_is_refused_and_never_attempted(lfs_repo: Path, key: str) -> None:
    # git-lfs applies url.*.insteadOf to the pinned lfs.url, so a repo could send
    # the upload to a host (or ssh command) of its choosing.
    pwned = lfs_repo.parent / "PWNED"
    ssh = lfs_repo.parent / "ssh.sh"
    ssh.write_text(f"#!/bin/sh\ntouch {pwned}\nexit 1\n")
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    _g(lfs_repo, "config", "core.sshCommand", str(ssh))
    _g(
        lfs_repo,
        "config",
        f"url.ssh://evil.invalid/.{key}",
        "https://github.com/o/r.git/",
    )
    seen: list[list[str]] = []

    def rec(argv: list[str], cwd: str | None = None) -> Any:
        seen.append(argv)
        if Rec.kind(argv) == "push":
            return CmdResult(0, "", "")
        return pr_endgame._default_runner(argv, cwd)

    out = _push_with_lfs(_PUSH, "HEAD", str(lfs_repo), rec)
    assert not out.ok
    assert not pwned.exists()
    kinds = [Rec.kind(a) for a in seen]
    assert "upload" not in kinds
    assert "push" not in kinds
