#!/usr/bin/env python3
"""AIFactory#1366 — the backend's git credentials must never reach argv.

``/proc/<pid>/cmdline`` is world-readable on Linux, so a push URL carrying
``x-access-token:<token>@`` publishes that token to every uid on the host for as
long as the ``git`` child lives. These tests pin the fix at three levels:

* :func:`test_real_git_child_never_publishes_the_token_in_proc_cmdline` spawns a
  REAL ``git`` child against a socket that accepts and never speaks, so the
  child is still blocked in the exchange while ``/proc`` is read — the read is
  not racing the child's exit. It asserts on the argv we constructed AND on
  ``/proc/<pid>/cmdline`` as the kernel published it. Those two halves are
  separate statements so each fires on its own.
* A **vacuity guard** asserts the token DID reach ``GIT_PASS``. A mutation that
  simply drops the credential fails as "never reached the env" rather than
  passing an argv check trivially.
* The two production call sites (``core.workspace_fetch`` and
  ``pfactory.tfactory_client``) are checked for the same property on the argv and
  env they actually hand to ``subprocess.run``.

Plus the subtlety carried over from PFactory#616: moving the credential into the
environment must not make it UNCONDITIONAL. A non-github remote gets no askpass
vars at all.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Self-sufficient sys.path insert: this module must collect ALONE, without
# free-riding on another test module's (or conftest's) insert.
_BACKEND = Path(__file__).resolve().parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

TOKEN = "ghp_1366SentinelTokenValueDoNotLeakMe"  # gitleaks:allow - test sentinel


@pytest.fixture
def dead_socket():
    """A listener that accepts connections and then says nothing, ever.

    A ``git`` child pointed at it blocks in the HTTPS exchange instead of
    failing fast, which is what keeps ``/proc/<pid>/cmdline`` readable while we
    look at it.
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            held.append(conn)  # never write, never close

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in held:
            conn.close()
        srv.close()


def test_real_git_child_never_publishes_the_token_in_proc_cmdline(
    dead_socket, monkeypatch, tmp_path
):
    """The kernel's own view of a live ``git`` child must not contain the token."""
    from core import git_credentials

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    # Redirect "the host this module credentials" at the dead socket so a REAL
    # git child does the connecting. Everything else is the production path.
    host = f"https://127.0.0.1:{dead_socket}/"
    monkeypatch.setattr(git_credentials, "GITHUB_HTTPS_PREFIX", host)

    with git_credentials.authed_push_url(f"{host}owner/repo.git") as (url, env):
        # Vacuity guard: if a mutation removes the credential entirely, this
        # fails first and the argv assertions below never get to pass trivially.
        assert env.get("GIT_PASS") == TOKEN, "token never reached the askpass env"
        assert env.get("GIT_ASKPASS"), "no askpass helper was wired"

        argv = ["git", "ls-remote", url, "main"]
        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=str(tmp_path),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # Half 1 — the argv we constructed.
            assert TOKEN not in " ".join(argv)

            # Half 2 — argv as the KERNEL published it, while the child is alive.
            cmdline = b""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
                except OSError:  # pragma: no cover - child exited underneath us
                    break
                if cmdline:
                    break
                time.sleep(0.05)
            assert cmdline, "never read a live /proc/<pid>/cmdline for the git child"
            assert TOKEN.encode() not in cmdline
            assert b"x-access-token@" in cmdline, "URL lost its username"
        finally:
            proc.kill()
            proc.wait(timeout=10)


def test_credential_is_not_offered_to_a_host_the_module_did_not_build(monkeypatch):
    """PFactory#616's subtlety: env credentials must not become unconditional."""
    from core import git_credentials

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    with git_credentials.authed_push_url("https://evil.example.com/o/r.git") as (
        url,
        env,
    ):
        assert url == "https://evil.example.com/o/r.git"
        assert "GIT_ASKPASS" not in env
        assert "GIT_PASS" not in env

    # ...and the github.com remote in the same process still IS credentialed,
    # so the assertion above is not passing because nothing works at all.
    with git_credentials.authed_push_url("https://github.com/o/r.git") as (url, env):
        assert url == "https://x-access-token@github.com/o/r.git"
        assert env["GIT_PASS"] == TOKEN


def test_askpass_helper_is_owner_only_and_removed_after_use(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    from core import git_credentials

    with git_credentials.authed_push_url("https://github.com/o/r.git") as (_, env):
        script = Path(env["GIT_ASKPASS"])
        assert script.stat().st_mode & 0o777 == 0o700
        assert script.read_text() == git_credentials._ASKPASS_SCRIPT
    assert not script.exists()


def test_workspace_fetch_push_keeps_the_token_out_of_argv(monkeypatch, tmp_path):
    """``core.workspace_fetch.maybe_push_workspace_branch`` call-site wiring."""
    from core import workspace_fetch as wf

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://bucket/key")
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / "042-x"
    wt.mkdir(parents=True)

    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def _fake_run(argv, **kw):
        calls.append((list(argv), kw.get("env")))
        out = {
            "rev-parse": "feature/x",
            "get-url": "https://github.com/o/r.git",
        }.get(argv[1] if len(argv) > 1 else "", "")
        if argv[1:3] == ["remote", "get-url"]:
            out = "https://github.com/o/r.git"
        return MagicMock(returncode=0, stdout=out, stderr="")

    with patch.object(subprocess, "run", side_effect=_fake_run):
        assert wf.maybe_push_workspace_branch(tmp_path, "042-x") is True

    push = next(c for c in calls if c[0][1] == "push")
    argv, env = push
    assert TOKEN not in " ".join(argv)
    assert "https://x-access-token@github.com/o/r.git" in argv
    assert env is not None and env["GIT_PASS"] == TOKEN  # vacuity guard


def test_tfactory_handoff_push_keeps_the_token_out_of_argv(monkeypatch, tmp_path):
    """``pfactory.tfactory_client._git_info_and_push`` call-site wiring."""
    from pfactory import tfactory_client as tc

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    spec_dir = tmp_path / "specs" / "042-x"
    spec_dir.mkdir(parents=True)
    repo = tc._project_dir(spec_dir)
    repo.mkdir(parents=True, exist_ok=True)

    sha = "a" * 40
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def _fake_run(argv, **kw):
        calls.append((list(argv), kw.get("env")))
        if argv[1:3] == ["remote", "get-url"]:
            return MagicMock(returncode=0, stdout="https://github.com/o/r.git\n")
        if argv[1] == "rev-parse":
            return MagicMock(returncode=0, stdout=f"{sha}\n")
        if argv[1] == "ls-remote":
            return MagicMock(returncode=0, stdout=f"{sha}\trefs/heads/x\n")
        return MagicMock(returncode=0, stdout="")

    with patch.object(subprocess, "run", side_effect=_fake_run):
        url, branch = tc._git_info_and_push(spec_dir, "042-x")

    assert branch, "handoff did not resolve a build branch"
    for argv, env in calls:
        assert TOKEN not in " ".join(argv), f"token leaked into argv: {argv[:2]}"
    for verb in ("push", "ls-remote"):
        argv, env = next(c for c in calls if c[0][1] == verb)
        assert "https://x-access-token@github.com/o/r.git" in argv
        assert env is not None and env["GIT_PASS"] == TOKEN  # vacuity guard
    assert url == "https://github.com/o/r.git"


def test_no_stray_token_urls_remain_in_the_backend():
    """A fleet-grep regression: the ``user:<token>@`` shape must not come back.

    Matches an INTERPOLATION right after the username, which is the leak; prose
    that merely mentions the shape is not.
    """
    leak = re.compile(r"x-access-token:[^\"'\s]*\{")
    hits = [
        f"{path}:{n}"
        for path in _BACKEND.rglob("*.py")
        if "__pycache__" not in str(path) and not path.name.startswith("test_")
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1)
        if leak.search(line)
    ]
    assert hits == [], f"credentialed URL rebuilt in argv-bound code: {hits}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
