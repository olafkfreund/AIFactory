#!/usr/bin/env python3
"""
Regression: stop kills the whole process tree, not just run.py (#stop-resistance)
=================================================================================

Builds spawn run.py with start_new_session=True; run.py then spawns coder
(Claude SDK) + git subprocesses. stop_task previously called proc.terminate(),
which signalled only run.py — the children orphaned and the build kept running
(stop/recover appeared to succeed but the work continued). _signal_process_tree
must signal the process GROUP, falling back to the single process.
"""

import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "web-server"))

from server.services.agent_service import AgentService  # noqa: E402


class _Proc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.signalled = []

    def send_signal(self, sig):
        self.signalled.append(sig)


def test_signals_process_group(monkeypatch):
    import server.services.agent_service as mod
    calls = {}
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: 9000 + pid)
    monkeypatch.setattr(mod.os, "killpg", lambda pgid, sig: calls.setdefault("killpg", (pgid, sig)))

    proc = _Proc(pid=4242)
    AgentService._signal_process_tree(proc, signal.SIGTERM)

    assert calls["killpg"] == (9000 + 4242, signal.SIGTERM)
    assert proc.signalled == []  # group kill → no single-proc fallback


def test_falls_back_to_single_process(monkeypatch):
    import server.services.agent_service as mod
    monkeypatch.setattr(mod.os, "getpgid", lambda pid: pid)

    def _boom(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(mod.os, "killpg", _boom)

    proc = _Proc()
    AgentService._signal_process_tree(proc, signal.SIGKILL)
    assert proc.signalled == [signal.SIGKILL]  # fell back to proc.send_signal


def test_no_pid_is_noop():
    proc = _Proc()
    proc.pid = None
    AgentService._signal_process_tree(proc, signal.SIGTERM)  # must not raise
    assert proc.signalled == []


def test_build_spawn_uses_new_session():
    # The build spawn must request a new session so a process group exists to
    # kill. Admission control (RFC-0016 #668) split start_task_execution into a
    # thin admission wrapper plus _spawn_task_execution, which now owns the
    # asyncio.create_subprocess_exec call — so inspect the spawn-bearing method.
    import inspect
    src = inspect.getsource(AgentService._spawn_task_execution)
    assert "start_new_session=True" in src


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
