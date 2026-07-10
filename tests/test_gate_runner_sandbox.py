"""Flag-gated factory-sandbox routing for trailing gates (#61 runtime adoption).

Default (flag off) must be the host runner — zero behavior change. With
AIFACTORY_SANDBOX_GATES + AIFACTORY_SANDBOX_IMAGE set, gates run via the vendored
factory-sandbox. These tests mock the sandbox (no real container).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents import gate_runner  # noqa: E402
from agents.gate_runner import _default_runner, _select_runner  # noqa: E402


def test_flag_off_uses_host_runner(monkeypatch):
    monkeypatch.delenv("AIFACTORY_SANDBOX_GATES", raising=False)
    monkeypatch.delenv("AIFACTORY_SANDBOX_IMAGE", raising=False)
    assert _select_runner() is _default_runner


def test_flag_on_without_image_falls_back_to_host(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.delenv("AIFACTORY_SANDBOX_IMAGE", raising=False)
    assert _select_runner() is _default_runner


class _FakeResult:
    def __init__(self, exit_code, output):
        self.exit_code, self.output = exit_code, output


def _install_fake_sandbox(monkeypatch, exit_code, output):
    calls = {}
    import core.factory_sandbox as fs

    class FakeSandbox:
        def __init__(self, image, **kw):
            calls["image"], calls["init_kw"] = image, kw

        def run(self, commands, **kw):
            calls["commands"], calls["run_kw"] = commands, kw
            return _FakeResult(exit_code, output)

    monkeypatch.setattr(fs, "FactorySandbox", FakeSandbox)
    return calls


def test_flag_on_with_image_routes_to_sandbox(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "true")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "ghcr.io/x/rust:1.90")
    calls = _install_fake_sandbox(monkeypatch, exit_code=0, output="ok")

    runner = _select_runner()
    assert runner is not _default_runner
    code, out = runner(["cargo", "test", "--all"], Path("/work/spec"))

    assert code == 0 and out == "ok"
    assert calls["image"] == "ghcr.io/x/rust:1.90"
    assert calls["init_kw"]["repo_rw"] is True  # worktree mounted rw for the build
    assert calls["commands"] == [
        "cargo test --all"
    ]  # argv shlex-joined to a shell line
    assert calls["run_kw"]["workdir"] == "/work/spec"


def test_missing_tool_exit_127_maps_to_skipped(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "img")
    _install_fake_sandbox(monkeypatch, exit_code=127, output="cargo: not found")
    code, _ = _select_runner()(["cargo", "test"], Path("/w"))
    assert (
        code is None
    )  # None => skipped, matching the host runner's missing-tool semantics


def test_sandbox_error_is_a_gate_failure_not_a_crash(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "img")
    import core.factory_sandbox as fs

    class Boom:
        def __init__(self, *a, **k): ...
        def run(self, *a, **k):
            raise RuntimeError("no runtime")

    monkeypatch.setattr(fs, "FactorySandbox", Boom)
    code, out = _select_runner()(["x"], Path("/w"))
    assert code == 1 and "factory-sandbox error" in out


@pytest.mark.asyncio
async def test_run_gates_still_honors_injected_runner(monkeypatch):
    # Back-compat: an explicitly injected runner is used regardless of the flag.
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "img")
    seen = []

    def fake_runner(cmd, cwd):
        seen.append(cmd)
        return 0, "ok"

    gates = [gate_runner.Gate(name="t", command=["echo", "hi"])]
    results = await gate_runner.run_gates(Path("/w"), gates, runner=fake_runner)
    assert seen == [["echo", "hi"]] and results[0].passed
