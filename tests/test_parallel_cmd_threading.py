#!/usr/bin/env python3
"""
Threading test for #376
=======================

Proves the parallel flags reach the spawned run.py command. The live benchmark
showed a build run serially despite parallel=true; this locks down the
route→agent_service→argv mapping so a regression can't silently drop the flags
again. (The route-level resolution + CLI argparse are covered elsewhere; this
covers the agent_service command construction.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.services.agent_service import _append_parallel_flags  # noqa: E402


def test_parallel_with_workers_appends_both_flags():
    cmd = ["python", "run.py", "--spec", "001"]
    added = _append_parallel_flags(cmd, True, 4)
    assert added is True
    assert "--parallel" in cmd
    assert cmd[cmd.index("--workers") + 1] == "4"


def test_parallel_without_workers_appends_only_flag():
    cmd = ["python", "run.py"]
    added = _append_parallel_flags(cmd, True, None)
    assert added is True
    assert "--parallel" in cmd
    assert "--workers" not in cmd


def test_not_parallel_appends_nothing():
    cmd = ["python", "run.py"]
    assert _append_parallel_flags(cmd, False, 4) is False
    assert _append_parallel_flags(cmd, None, 4) is False
    assert cmd == ["python", "run.py"]


def test_zero_or_negative_workers_skips_workers_flag():
    cmd = ["python", "run.py"]
    _append_parallel_flags(cmd, True, 0)
    assert "--parallel" in cmd and "--workers" not in cmd


def test_run_autonomous_agent_accepts_flags():
    """The CLI end of the chain: the executor entrypoint takes parallel/workers."""
    import inspect

    sys.path.insert(0, str(_WEB_SERVER.parent / "backend"))
    from agents.coder import run_autonomous_agent

    params = inspect.signature(run_autonomous_agent).parameters
    assert "parallel" in params
    assert "workers" in params


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
