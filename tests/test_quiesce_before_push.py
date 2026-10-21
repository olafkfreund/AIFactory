"""Build Jobs clear same-uid agent processes before pushing (#1724)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

from core import process_hardening, workspace_fetch  # noqa: E402


@pytest.fixture(autouse=True)
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires /proc")
def test_stray_pids_excludes_ancestors_and_finds_child() -> None:
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)  # noqa: S607
    try:
        strays = process_hardening._stray_pids()
        assert child.pid in strays
        assert os.getpid() not in strays
        assert os.getppid() not in strays
    finally:
        child.kill()
        child.wait()


def test_kill_stray_processes_rechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = 123456
    seen: list[tuple[int, signal.Signals]] = []
    scans = iter([[pid], []])
    monkeypatch.setattr(process_hardening, "_stray_pids", lambda: next(scans))
    monkeypatch.setattr(os, "kill", lambda p, sig: seen.append((p, sig)))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert process_hardening.kill_stray_processes() is True
    assert seen == [(pid, signal.SIGKILL)]


def test_kill_stray_processes_refuses_survivors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 123456
    seen: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(process_hardening, "_stray_pids", lambda: [pid])
    monkeypatch.setattr(os, "kill", lambda p, sig: seen.append((p, sig)))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert process_hardening.kill_stray_processes() is False
    assert seen == [(pid, signal.SIGKILL)] * 5


def test_job_refuses_push_when_process_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/workspace")
    monkeypatch.setenv("JOB_ID", "job-1")
    monkeypatch.setenv("FACTORY_SERVICE", "build")
    (tmp_path / ".aifactory" / "worktrees" / "tasks" / "042-x").mkdir(parents=True)
    monkeypatch.setattr(process_hardening, "kill_stray_processes", lambda: False)

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("git must not run after quiescence fails")

    monkeypatch.setattr(subprocess, "run", unexpected_run)
    assert workspace_fetch.maybe_push_workspace_branch(tmp_path, "042-x") is False
    assert "agent-started processes survived; refusing to push (#1724)" in caplog.text


def test_non_job_skips_quiescence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WORKSPACE_URI", "s3://bucket/workspace")
    monkeypatch.delenv("JOB_ID", raising=False)
    monkeypatch.setenv("FACTORY_SERVICE", "build")
    (tmp_path / ".aifactory" / "worktrees" / "tasks" / "042-x").mkdir(parents=True)

    def unexpected_kill() -> bool:
        raise AssertionError("local push must not quiesce processes")

    monkeypatch.setattr(process_hardening, "kill_stray_processes", unexpected_kill)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=""),
    )
    assert workspace_fetch.maybe_push_workspace_branch(tmp_path, "042-x") is False
