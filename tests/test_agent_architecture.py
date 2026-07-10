#!/usr/bin/env python3
"""
Tests for Agent Architecture
============================

Verifies the agent architecture where:
- A Python orchestrator runs Claude SDK / provider sessions
- The agent may still spawn subagents internally (via the Task tool)
- Independent subtasks of a ``parallel_safe`` phase are additionally run
  concurrently by the executor in dependency-graph waves (#376)

History (#376): an earlier design forbade ALL Python-level parallel
orchestration and relied solely on the agent's own Task-tool subagents to
parallelize. Empirically that never produced concurrency (builds ran strictly
serially), so #376 introduced provider-agnostic executor-level wave scheduling
behind an opt-in ``--parallel``/``--workers`` flag. The legacy
``coordinator.py``/``task_tool.py`` modules are still intentionally absent — the
new orchestration lives in ``agents/parallel_runner.py`` (pure scheduling) and
``agents/parallel_integration.py`` (worktree/session wiring).
"""

import ast
import inspect
import sys
from pathlib import Path

import pytest

# Add apps/backend directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))


class TestNoExternalParallelism:
    """Verify the legacy orchestration modules stay absent (#376).

    Executor-level parallelism now lives in ``agents/parallel_runner.py`` and
    ``agents/parallel_integration.py``; the old ``coordinator.py`` /
    ``task_tool.py`` designs were never adopted and must not reappear.
    """

    def test_no_coordinator_module(self):
        """No legacy coordinator module should exist."""
        coordinator_path = (
            Path(__file__).parent.parent / "apps" / "backend" / "coordinator.py"
        )
        assert not coordinator_path.exists(), (
            "coordinator.py should not exist. Wave orchestration lives in "
            "agents/parallel_runner.py (scheduling) + parallel_integration.py."
        )

    def test_no_task_tool_module(self):
        """No legacy task_tool wrapper module should exist."""
        task_tool_path = (
            Path(__file__).parent.parent / "apps" / "backend" / "task_tool.py"
        )
        assert not task_tool_path.exists(), (
            "task_tool.py should not exist. The agent spawns subagents directly "
            "using Claude Code's built-in Task tool."
        )

    def test_no_subtask_worker_config(self):
        """No external subtask worker agent config should exist."""
        worker_config = (
            Path(__file__).parent.parent / ".claude" / "agents" / "subtask-worker.md"
        )
        assert not worker_config.exists(), (
            "subtask-worker.md should not exist. Subagents use Claude Code's "
            "built-in agent types, not custom configs."
        )


class TestCLIInterface:
    """Verify the CLI exposes the opt-in parallel execution flags (#376)."""

    def test_parallel_flag_exposed(self):
        """CLI should define --parallel and --workers arguments."""
        main_py_path = (
            Path(__file__).parent.parent / "apps" / "backend" / "cli" / "main.py"
        )
        content = main_py_path.read_text()

        assert '"--parallel"' in content, (
            "CLI should expose --parallel for dependency-graph wave execution (#376)."
        )
        assert '"--workers"' in content, (
            "CLI should expose --workers to cap concurrent subtasks (#376)."
        )


class TestAgentEntryPoint:
    """Verify the agent entry point function signature."""

    def test_accepts_parallel_parameters(self):
        """Agent entry point accepts the #376 opt-in parallelism config."""
        from agent import run_autonomous_agent

        sig = inspect.signature(run_autonomous_agent)
        param_names = list(sig.parameters.keys())

        assert "parallel" in param_names, (
            "Agent should accept a 'parallel' parameter (#376 executor waves)."
        )
        assert "workers" in param_names, (
            "Agent should accept a 'workers' parameter to cap wave concurrency."
        )

    def test_required_parameters(self):
        """Agent entry point has required parameters."""
        from agent import run_autonomous_agent

        sig = inspect.signature(run_autonomous_agent)
        param_names = list(sig.parameters.keys())

        expected = ["project_dir", "spec_dir", "model"]
        for param in expected:
            assert param in param_names, f"Expected parameter '{param}' not found"

    def test_is_async(self):
        """Agent entry point is async."""
        from agent import run_autonomous_agent

        assert inspect.iscoroutinefunction(run_autonomous_agent), (
            "run_autonomous_agent should be async"
        )


class TestAgentPrompt:
    """Verify the agent prompt documents subagent capability."""

    def test_mentions_subagents(self):
        """Agent prompt mentions subagent capability."""
        coder_prompt_path = (
            Path(__file__).parent.parent / "apps" / "backend" / "prompts" / "coder.md"
        )
        content = coder_prompt_path.read_text()

        assert "subagent" in content.lower(), (
            "Agent prompt should document subagent capability for parallel work."
        )

    def test_mentions_parallel_capability(self):
        """Agent prompt mentions parallel/concurrent capability."""
        coder_prompt_path = (
            Path(__file__).parent.parent / "apps" / "backend" / "prompts" / "coder.md"
        )
        content = coder_prompt_path.read_text()

        has_task_tool = "task tool" in content.lower() or "Task tool" in content
        has_parallel = "parallel" in content.lower()
        has_concurrent = (
            "concurrent" in content.lower() or "simultaneously" in content.lower()
        )

        assert has_task_tool or has_parallel or has_concurrent, (
            "Agent prompt should mention parallel/concurrent work capability."
        )


class TestModuleIntegrity:
    """Verify core modules work correctly."""

    def test_agent_module_imports(self):
        """Agent module imports without errors."""
        try:
            import agent
        except ImportError as e:
            pytest.fail(f"agent.py failed to import: {e}")

    def test_run_module_valid_syntax(self):
        """Run module has valid Python syntax."""
        run_py_path = Path(__file__).parent.parent / "apps" / "backend" / "run.py"
        content = run_py_path.read_text()

        try:
            ast.parse(content)
        except SyntaxError as e:
            pytest.fail(f"run.py has syntax error: {e}")

    def test_no_coordinator_imports(self):
        """Core modules don't import coordinator."""
        for filename in ["run.py", "core/agent.py"]:
            filepath = Path(__file__).parent.parent / "apps" / "backend" / filename
            content = filepath.read_text()

            assert "from coordinator import" not in content, (
                f"{filename} should not import coordinator"
            )
            assert "import coordinator" not in content, (
                f"{filename} should not import coordinator"
            )

    def test_no_task_tool_imports(self):
        """Core modules don't import task_tool."""
        for filename in ["run.py", "core/agent.py"]:
            filepath = Path(__file__).parent.parent / "apps" / "backend" / filename
            content = filepath.read_text()

            assert "from task_tool import" not in content, (
                f"{filename} should not import task_tool"
            )
            assert "import task_tool" not in content, (
                f"{filename} should not import task_tool"
            )


class TestProjectDocumentation:
    """Verify project documentation is accurate."""

    def test_parallel_orchestration_modules_exist(self):
        """The #376 executor-level wave modules are present."""
        backend = Path(__file__).parent.parent / "apps" / "backend"
        assert (backend / "agents" / "parallel_runner.py").exists(), (
            "agents/parallel_runner.py (pure wave scheduler) should exist (#376)."
        )
        assert (backend / "agents" / "parallel_integration.py").exists(), (
            "agents/parallel_integration.py (worktree/session wiring) should exist (#376)."
        )

    def test_subagent_architecture_documented(self):
        """CLAUDE.md documents subagent-based architecture."""
        claude_md_path = Path(__file__).parent.parent / "CLAUDE.md"
        content = claude_md_path.read_text(encoding="utf-8")

        has_subagent = "subagent" in content.lower()
        has_task_tool = "task tool" in content.lower()

        assert has_subagent or has_task_tool, (
            "CLAUDE.md should document subagent-based parallel work"
        )


class TestElectronToolScoping:
    """Verify Electron MCP tools are scoped to QA agents only."""

    def test_coder_no_electron_tools(self, monkeypatch):
        """Coder should NOT get Electron tools even when enabled and project is Electron."""
        monkeypatch.setenv("ELECTRON_MCP_ENABLED", "true")

        from aifactory_tools import get_allowed_tools

        # Even with is_electron=True, coder should not get Electron tools
        coder_tools = get_allowed_tools(
            "coder", project_capabilities={"is_electron": True}
        )

        has_electron = any("electron" in tool.lower() for tool in coder_tools)
        assert not has_electron, (
            "Coder should NOT have Electron tools - they are scoped to QA agents only. "
            "This prevents context token bloat for agents that don't need desktop automation."
        )

    def test_planner_no_electron_tools(self, monkeypatch):
        """Planner should NOT get Electron tools even when enabled and project is Electron."""
        monkeypatch.setenv("ELECTRON_MCP_ENABLED", "true")

        from aifactory_tools import get_allowed_tools

        # Even with is_electron=True, planner should not get Electron tools
        planner_tools = get_allowed_tools(
            "planner", project_capabilities={"is_electron": True}
        )

        has_electron = any("electron" in tool.lower() for tool in planner_tools)
        assert not has_electron, (
            "Planner should NOT have Electron tools - they are scoped to QA agents only. "
            "This prevents context token bloat for agents that don't need desktop automation."
        )

    def test_no_electron_tools_when_disabled(self, monkeypatch):
        """No agent gets Electron tools when ELECTRON_MCP_ENABLED is not set."""
        monkeypatch.delenv("ELECTRON_MCP_ENABLED", raising=False)

        from aifactory_tools import get_allowed_tools

        for agent_type in ["planner", "coder", "qa_reviewer", "qa_fixer"]:
            # Even with is_electron=True, no tools without env var
            tools = get_allowed_tools(
                agent_type, project_capabilities={"is_electron": True}
            )
            has_electron = any("electron" in tool.lower() for tool in tools)
            assert not has_electron, (
                f"{agent_type} should NOT have Electron tools when ELECTRON_MCP_ENABLED is not set"
            )


class TestSubtaskTerminology:
    """Verify subtask terminology is used consistently."""

    def test_progress_uses_subtask_terminology(self):
        """Progress module uses subtask terminology."""
        progress_path = (
            Path(__file__).parent.parent / "apps" / "backend" / "core" / "progress.py"
        )
        content = progress_path.read_text()

        assert "subtask" in content.lower(), (
            "core/progress.py should use subtask terminology"
        )


def run_tests():
    """Run all tests when executed directly."""
    print("\nTesting Agent Architecture")
    print("=" * 60)

    test_classes = [
        TestNoExternalParallelism,
        TestCLIInterface,
        TestAgentEntryPoint,
        TestAgentPrompt,
        TestModuleIntegrity,
        TestProjectDocumentation,
        TestElectronToolScoping,  # Note: requires pytest (uses monkeypatch)
        TestSubtaskTerminology,
    ]

    passed = 0
    failed = 0

    for test_class in test_classes:
        print(f"\n{test_class.__name__}:")
        instance = test_class()

        for method_name in dir(instance):
            if method_name.startswith("test_"):
                method = getattr(instance, method_name)
                try:
                    method()
                    print(f"  ✓ {method_name}")
                    passed += 1
                except AssertionError as e:
                    print(f"  ✗ {method_name}: {e}")
                    failed += 1
                except Exception as e:
                    print(f"  ✗ {method_name}: Unexpected error: {e}")
                    failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_tests())
