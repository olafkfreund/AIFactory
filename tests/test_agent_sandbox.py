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
    # Installed AND functional (probe returns True) — the happy path.
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_bwrap_works", lambda _b: True)


@pytest.fixture
def bwrap_absent(monkeypatch):
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: None)


@pytest.fixture
def bwrap_broken(monkeypatch):
    # Installed but can't create a namespace (node without unprivileged userns) —
    # #991: this must degrade to passthrough, not fail every wrapped command.
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_bwrap_works", lambda _b: False)


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

    def test_bwrap_present_but_broken_is_passthrough(self, monkeypatch, bwrap_broken):
        """#991: bwrap installed but can't create a namespace (no unprivileged
        userns) must degrade to an unwrapped passthrough — NOT wrap the command
        with a bwrap that exits non-zero and fails every git commit."""
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "fs")
        assert sandbox.is_enabled() is False
        assert sandbox.build_sandboxed_command(CMD, ROOT) == CMD  # unwrapped


class TestWrapped:
    def test_fs_mode_wraps_correctly(self, monkeypatch, bwrap_present):
        monkeypatch.setenv("AIFACTORY_AGENT_SANDBOX", "fs")
        out = sandbox.build_sandboxed_command(CMD, ROOT)

        assert out[0] == "/usr/bin/bwrap"
        assert out[-len(CMD) :] == CMD  # real command at the tail
        assert out[-len(CMD) - 1] == "--"  # after the `--` separator
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


class TestBwrapProbe:
    """The real `_bwrap_works` probe (not the monkeypatched fixtures above):
    logging on failure, and which failures get cached for the process
    lifetime vs. retried (#997 — a silent, permanently-disabled sandbox)."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        sandbox._bwrap_cache.clear()
        yield
        sandbox._bwrap_cache.clear()

    def test_exception_is_logged_and_not_cached(self, monkeypatch, caplog):
        calls = []

        def _boom(*a, **k):
            calls.append(1)
            raise TimeoutError("probe timed out")

        monkeypatch.setattr(sandbox.subprocess, "run", _boom)
        with caplog.at_level("WARNING"):
            assert sandbox._bwrap_works("/usr/bin/bwrap") is False
            assert sandbox._bwrap_works("/usr/bin/bwrap") is False

        assert len(calls) == 2  # not cached — probed again on the 2nd call
        assert any("bwrap probe failed to run" in r.message for r in caplog.records)
        assert any("TimeoutError" in r.message for r in caplog.records)

    def test_nonzero_returncode_is_cached(self, monkeypatch, caplog):
        calls = []

        class _Result:
            returncode = 1
            stderr = b"No permissions to create a new namespace"

        def _run(*a, **k):
            calls.append(1)
            return _Result()

        monkeypatch.setattr(sandbox.subprocess, "run", _run)
        with caplog.at_level("WARNING"):
            assert sandbox._bwrap_works("/usr/bin/bwrap") is False
            assert sandbox._bwrap_works("/usr/bin/bwrap") is False

        assert len(calls) == 1  # deterministic failure — cached, no re-probe
        assert any("cannot create a namespace" in r.message for r in caplog.records)

    def test_success_is_cached(self, monkeypatch):
        calls = []

        class _Result:
            returncode = 0
            stderr = b""

        def _run(*a, **k):
            calls.append(1)
            return _Result()

        monkeypatch.setattr(sandbox.subprocess, "run", _run)
        assert sandbox._bwrap_works("/usr/bin/bwrap") is True
        assert sandbox._bwrap_works("/usr/bin/bwrap") is True
        assert len(calls) == 1


def _pair(args, flag):
    i = args.index(flag)
    return (args[i + 1], args[i + 2])


def _has_triplet(args, flag, a, b):
    for i, t in enumerate(args):
        if t == flag and args[i + 1 : i + 3] == [a, b]:
            return True
    return False
