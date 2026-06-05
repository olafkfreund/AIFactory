#!/usr/bin/env python3
"""
fs/process validator hardening (#364 — epic #318)
=================================================

`rm`/`chmod` must resolve targets against the worktree root and reject escapes
(absolute-outside, `..`, `--no-preserve-root`); `kill`/`pkill`/`killall` must
reject process-group / all-process targets. Ordinary in-worktree ops still pass.
"""

from __future__ import annotations

import pytest
from security.exec_context import (
    reset_worktree_root,
    set_worktree_root,
    target_escapes_worktree,
)
from security.filesystem_validators import (
    validate_chmod_command,
    validate_rm_command,
)
from security.process_validators import validate_kill_command

ROOT = "/work/proj"


@pytest.fixture
def worktree():
    tok = set_worktree_root(ROOT)
    yield ROOT
    reset_worktree_root(tok)


class TestRmContainment:
    @pytest.mark.parametrize("cmd", [
        "rm -rf build",
        "rm -rf ./src/tmp",
        "rm /work/proj/nested/a.txt",       # absolute but inside the worktree
        "rm file.txt",
    ])
    def test_in_worktree_allowed(self, cmd, worktree):
        ok, _ = validate_rm_command(cmd)
        assert ok is True

    @pytest.mark.parametrize("cmd", [
        "rm -rf /etc/passwd",               # absolute, outside
        "rm -rf ../../etc",                 # .. escape
        "rm /work/other/x",                 # sibling dir
        "rm -rf --no-preserve-root /",      # the rm -rf / re-enabler
    ])
    def test_escapes_blocked(self, cmd, worktree):
        ok, _ = validate_rm_command(cmd)
        assert ok is False


class TestChmodContainment:
    def test_in_worktree_allowed(self, worktree):
        assert validate_chmod_command("chmod +x scripts/run.sh")[0] is True

    @pytest.mark.parametrize("cmd", [
        "chmod -R 755 /etc",
        "chmod +x ../escape.sh",
        "chmod 755 /work/other/x",
    ])
    def test_outside_blocked(self, cmd, worktree):
        assert validate_chmod_command(cmd)[0] is False


class TestKillScoping:
    @pytest.mark.parametrize("cmd", ["kill 1234", "kill -9 1234", "kill -s TERM 555"])
    def test_positive_pid_allowed(self, cmd):
        assert validate_kill_command(cmd)[0] is True

    @pytest.mark.parametrize("cmd", [
        "kill -1", "kill 0", "kill -- -1234", "kill -9 -1", "kill -15 -1",
    ])
    def test_group_and_all_blocked(self, cmd):
        assert validate_kill_command(cmd)[0] is False


class TestTargetEscapesWorktree:
    def test_inside(self):
        assert target_escapes_worktree("a/b", "/work/proj") is False
        assert target_escapes_worktree("/work/proj/a", "/work/proj") is False

    def test_outside(self):
        assert target_escapes_worktree("/etc/passwd", "/work/proj") is True
        assert target_escapes_worktree("../x", "/work/proj") is True

    def test_no_root_fails_closed(self):
        # Unknown root → reject absolute + `..`, allow plain relative.
        assert target_escapes_worktree("/etc", None) is True
        assert target_escapes_worktree("../x", None) is True
        assert target_escapes_worktree("build/out", None) is False
