#!/usr/bin/env python3
"""
Identifier validation (#371 — epic #318)
========================================

Agent/CLI-supplied ``spec_name`` and ``task_id`` build filesystem paths and HTTP
URLs. These tests pin the independent allowlist validators (traversal/charset
rejected, legit values pass) and their application at the commit-message spec
lookup and the task-control MCP tools.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from security import validate_spec_name, validate_task_id  # noqa: E402

BAD_SPEC_NAMES = [
    "../../etc/passwd",
    "..",
    "a/b",
    "/abs",
    "\\win",
    ".hidden",
    "x" * 201,
    "",
]
GOOD_SPEC_NAMES = [
    "001-add-auth",
    "feature_x",
    "a",
    "v2.1-thing",
    "042-correction-receiver",
]

BAD_TASK_IDS = ["../../admin/reset", "a/b", "..", "proj/spec", "x" * 201, ""]
GOOD_TASK_IDS = ["proj:001-x", "task-123", "a", "my-project:042-correction-receiver"]


class TestValidateSpecName:
    @pytest.mark.parametrize("name", BAD_SPEC_NAMES)
    def test_rejects_unsafe(self, name):
        with pytest.raises(ValueError):
            validate_spec_name(name)

    @pytest.mark.parametrize("name", GOOD_SPEC_NAMES)
    def test_accepts_legit(self, name):
        assert validate_spec_name(name) == name


class TestValidateTaskId:
    @pytest.mark.parametrize("tid", BAD_TASK_IDS)
    def test_rejects_unsafe(self, tid):
        with pytest.raises(ValueError):
            validate_task_id(tid)

    @pytest.mark.parametrize("tid", GOOD_TASK_IDS)
    def test_accepts_legit(self, tid):
        assert validate_task_id(tid) == tid


class TestCommitMessageSpecDirContainment:
    def test_safe_spec_dir_rejects_traversal(self, tmp_path):
        from commit_message import _safe_spec_dir

        # An unsafe name yields None (no traversal path built) — graceful skip.
        assert _safe_spec_dir(tmp_path, "../../etc") is None

    def test_safe_spec_dir_resolves_valid(self, tmp_path):
        from commit_message import _safe_spec_dir

        d = tmp_path / ".aifactory" / "specs" / "001-x"
        d.mkdir(parents=True)
        assert _safe_spec_dir(tmp_path, "001-x") == d


class TestTaskControlToolRejectsBadId:
    def test_task_get_blocks_traversal_id(self, monkeypatch):
        from agents.tools_pkg.tools import task_control

        # Build the tool fns without the real SDK @tool decorator.
        captured = {}

        def fake_tool(name, desc, schema):
            def deco(fn):
                captured[name] = fn
                return fn

            return deco

        monkeypatch.setattr(task_control, "tool", fake_tool)
        monkeypatch.setattr(task_control, "SDK_TOOLS_AVAILABLE", True)
        task_control.create_task_control_tools()

        result = asyncio.run(captured["task_get"]({"task_id": "../../admin/reset"}))
        assert result.get("isError") is True
        assert "task_id" in result["content"][0]["text"]
