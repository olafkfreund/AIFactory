#!/usr/bin/env python3
"""
Tests for spec_runner forwarding parallel flags to the chained build
====================================================================

Root-caused live: spec_runner chains into the build via os.execv(run.py …) but
omitted --parallel/--workers, so create-and-run builds ran serial despite the
user choosing parallel (the #392 agent_service threading is bypassed by the
os.execv path). forward_parallel_flags reads task_metadata.json and forwards the
flags so the chained build actually runs in waves.
"""

import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

from runners.spec_runner import forward_parallel_flags  # noqa: E402


def _base_cmd():
    return ["python", "run.py", "--spec", "001-x", "--project-dir", "/p", "--auto-continue"]


def test_forwards_parallel_and_workers(tmp_path: Path):
    (tmp_path / "task_metadata.json").write_text(json.dumps({"parallel": True, "workers": 4}))
    cmd = forward_parallel_flags(_base_cmd(), tmp_path)
    assert "--parallel" in cmd
    assert cmd[cmd.index("--workers") + 1] == "4"


def test_parallel_without_workers(tmp_path: Path):
    (tmp_path / "task_metadata.json").write_text(json.dumps({"parallel": True}))
    cmd = forward_parallel_flags(_base_cmd(), tmp_path)
    assert "--parallel" in cmd
    assert "--workers" not in cmd


def test_parallel_false_adds_nothing(tmp_path: Path):
    (tmp_path / "task_metadata.json").write_text(json.dumps({"parallel": False, "workers": 4}))
    cmd = forward_parallel_flags(_base_cmd(), tmp_path)
    assert "--parallel" not in cmd


def test_no_metadata_file_is_serial(tmp_path: Path):
    cmd = forward_parallel_flags(_base_cmd(), tmp_path)
    assert "--parallel" not in cmd


def test_corrupt_metadata_is_serial(tmp_path: Path):
    (tmp_path / "task_metadata.json").write_text("{not json")
    cmd = forward_parallel_flags(_base_cmd(), tmp_path)
    assert "--parallel" not in cmd  # best-effort: degrade to serial, never crash


def test_zero_or_negative_workers_skips_workers_flag(tmp_path: Path):
    (tmp_path / "task_metadata.json").write_text(json.dumps({"parallel": True, "workers": 0}))
    cmd = forward_parallel_flags(_base_cmd(), tmp_path)
    assert "--parallel" in cmd and "--workers" not in cmd


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
