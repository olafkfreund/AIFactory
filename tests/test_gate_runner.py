#!/usr/bin/env python3
"""
Tests for the trailing-gate runner (#376 solution D)
====================================================

Covers gate detection from project marker files and the run/aggregation logic
(pass / fail / skipped-when-tool-missing), using an injected runner so no real
mypy/pytest/tsc processes are spawned.
"""

import pytest
from agents.gate_runner import (
    Gate,
    GateResult,
    detect_gates,
    failing_gates,
    run_gates,
    summarize_gates,
)

# asyncio_mode=auto (pytest.ini) auto-detects async tests.


class TestDetectGates:
    def test_python_mypy_and_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.mypy]\nstrict = true\n[tool.pytest.ini_options]\n"
        )
        (tmp_path / "tests").mkdir()
        names = [g.name for g in detect_gates(tmp_path)]
        assert "mypy" in names
        assert "pytest" in names

    def test_node_ts_and_scripts(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"scripts": {"lint": "eslint .", "test": "vitest"}}'
        )
        (tmp_path / "tsconfig.json").write_text("{}")
        names = [g.name for g in detect_gates(tmp_path)]
        assert {"tsc", "lint", "test"} <= set(names)

    def test_rust_and_go(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        (tmp_path / "go.mod").write_text("module x\n")
        names = [g.name for g in detect_gates(tmp_path)]
        assert "cargo-test" in names
        assert "go-test" in names

    def test_empty_project_no_gates(self, tmp_path):
        assert detect_gates(tmp_path) == []

    def test_pytest_from_tests_dir_only(self, tmp_path):
        (tmp_path / "tests").mkdir()
        names = [g.name for g in detect_gates(tmp_path)]
        assert names == ["pytest"]


class TestRunGates:
    async def test_all_pass(self, tmp_path):
        gates = [Gate("mypy", ["mypy", "."]), Gate("pytest", ["pytest", "-q"])]

        def runner(cmd, cwd):
            return 0, "ok"

        results = await run_gates(tmp_path, gates, runner=runner)
        assert all(r.passed and not r.skipped for r in results)
        assert failing_gates(results) == []
        assert summarize_gates(results) == "mypy: passed, pytest: passed"

    async def test_one_fails(self, tmp_path):
        gates = [Gate("mypy", ["mypy", "."]), Gate("pytest", ["pytest", "-q"])]

        def runner(cmd, cwd):
            return (0, "ok") if cmd[0] == "mypy" else (1, "2 failed")

        results = await run_gates(tmp_path, gates, runner=runner)
        failed = failing_gates(results)
        assert [r.name for r in failed] == ["pytest"]
        assert "FAILED" not in summarize_gates(results)  # uses lowercase 'failed'
        assert "pytest: failed" in summarize_gates(results)

    async def test_missing_tool_is_skipped_not_failed(self, tmp_path):
        gates = [Gate("mypy", ["mypy", "."])]

        def runner(cmd, cwd):
            return None, "command not found: mypy"

        results = await run_gates(tmp_path, gates, runner=runner)
        assert results[0].skipped
        assert results[0].passed  # skipped tools don't count as failures
        assert failing_gates(results) == []

    async def test_no_gates_detected_returns_empty(self, tmp_path):
        results = await run_gates(tmp_path, [], runner=lambda c, d: (0, ""))
        assert results == []

    async def test_output_tail_captured_on_failure(self, tmp_path):
        gates = [Gate("pytest", ["pytest", "-q"])]

        def runner(cmd, cwd):
            return 1, "E   assert 1 == 2"

        results = await run_gates(tmp_path, gates, runner=runner)
        assert results[0].exit_code == 1
        assert "assert 1 == 2" in results[0].output_tail

    async def test_autodetect_when_gates_omitted(self, tmp_path):
        (tmp_path / "tests").mkdir()
        called = []

        def runner(cmd, cwd):
            called.append(cmd[0])
            return 0, ""

        results = await run_gates(tmp_path, runner=runner)
        assert [r.name for r in results] == ["pytest"]
        assert called == ["pytest"]


class TestSummaries:
    def test_summarize_mixed(self):
        results = [
            GateResult("mypy", passed=True),
            GateResult("pytest", passed=False, exit_code=1),
            GateResult("tsc", passed=True, skipped=True),
        ]
        assert summarize_gates(results) == "mypy: passed, pytest: failed, tsc: skipped"

    def test_summarize_empty(self):
        assert summarize_gates([]) == "no gates detected"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
