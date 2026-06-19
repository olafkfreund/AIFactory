#!/usr/bin/env python3
"""
Optional agent OS sandbox wrapper (#363 slice 2 — epic #318)
============================================================

`build_sandboxed_command` wraps the agent subprocess with bubblewrap when
`AIFACTORY_AGENT_SANDBOX` is set AND `bwrap` is installed; otherwise it is a
pure passthrough (zero behaviour change by default). These tests pin both:
the passthrough paths, and the wrapped arg construction (RW worktree only,
RO system dirs, net isolation only in `strict`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "web-server"))

from server.services import sandbox  # noqa: E402

CMD = ["python", "run.py", "--spec", "001"]
ROOT = "/work/proj"


@pytest.fixture
def bwrap_present(monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")


@pytest.fixture
def bwrap_absent(monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: None)


class TestPassthrough:
    def test_default_off_is_passthrough(self, monkeypatch, bwrap_present):
        monkeypatch.delenv("AIFACTORY_AGENT_SANDBOX", raising=False)
        out = sandbox.build_sandboxed_command(CMD, ROOT)
        assert out == CMD
        assert sandbox.is_enabled() is False

    def test_explicit_off_is_passthrough(self, monkeypatch, bwrap_present):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "off")
        assert sandbox.build_sandboxed_command(CMD, ROOT) == CMD

    def test_bwrap_missing_is_passthrough(self, monkeypatch, bwrap_absent):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "fs")
        assert sandbox.build_sandboxed_command(CMD, ROOT) == CMD
        assert sandbox.is_enabled() is False


class TestWrapped:
    def test_fs_mode_wraps_correctly(self, monkeypatch, bwrap_present):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "fs")
        out = sandbox.build_sandboxed_command(CMD, ROOT)

        assert out[0] == "/usr/bin/bwrap"
        assert out[-len(CMD):] == CMD          # real command at the tail
        assert out[-len(CMD) - 1] == "--"      # after the `--` separator
        # RW only the worktree.
        assert _pair(out, "--bind") == (ROOT, ROOT)
        assert "--chdir" in out and out[out.index("--chdir") + 1] == ROOT
        # System dirs read-only.
        assert _has_triplet(out, "--ro-bind-try", "/usr", "/usr")
        assert "--tmpfs" in out
        # fs mode keeps the network.
        assert "--unshare-net" not in out
        assert sandbox.is_enabled() is True

    def test_strict_mode_isolates_network(self, monkeypatch, bwrap_present):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "strict")
        out = sandbox.build_sandboxed_command(CMD, ROOT)
        assert "--unshare-net" in out
        assert _pair(out, "--bind") == (ROOT, ROOT)

    def test_only_worktree_is_writable(self, monkeypatch, bwrap_present):
        # The only --bind (read-write) target is the worktree; system dirs use
        # --ro-bind-try.
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "fs")
        out = sandbox.build_sandboxed_command(CMD, ROOT)
        rw_targets = [out[i + 1] for i, t in enumerate(out) if t == "--bind"]
        assert rw_targets == [ROOT]

    def test_explicit_mode_arg_overrides_env(self, monkeypatch, bwrap_present):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "off")
        out = sandbox.build_sandboxed_command(CMD, ROOT, mode="fs")
        assert out[0] == "/usr/bin/bwrap"


class TestPidNamespace:
    """#363 AC1: PID isolation is opt-in so the default runs unprivileged in-pod.

    A fresh /proc (required by --unshare-pid) can't be mounted in an unprivileged
    k8s pod, which is what kept the sandbox inert on k3d. By default we keep the
    host PID namespace + a read-only /proc; --unshare-pid is opt-in.
    """

    def test_default_has_no_pid_namespace(self, monkeypatch, bwrap_present):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "fs")
        monkeypatch.delenv("AIFACTORY_AGENT_SANDBOX_PIDNS", raising=False)
        out = sandbox.build_sandboxed_command(CMD, ROOT)
        assert "--unshare-pid" not in out
        # /proc is exposed read-only (not a fresh procfs mount).
        assert _has_triplet(out, "--ro-bind-try", "/proc", "/proc")
        assert "--proc" not in out

    def test_pidns_opt_in_uses_fresh_proc(self, monkeypatch, bwrap_present):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "fs")
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX_PIDNS", "1")
        out = sandbox.build_sandboxed_command(CMD, ROOT)
        assert "--unshare-pid" in out
        assert "--proc" in out and out[out.index("--proc") + 1] == "/proc"
        # The opt-in path does NOT also read-only-bind the host /proc.
        assert not _has_triplet(out, "--ro-bind-try", "/proc", "/proc")


def _pair(args, flag):
    i = args.index(flag)
    return (args[i + 1], args[i + 2])


def _has_triplet(args, flag, a, b):
    for i, t in enumerate(args):
        if t == flag and args[i + 1 : i + 3] == [a, b]:
            return True
    return False
