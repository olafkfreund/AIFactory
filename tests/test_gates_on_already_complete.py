"""A build that is already complete on entry must still run its gates.

`run_autonomous_agent` checks `is_build_complete` before its session loop and
returned there — before the trailing gates that sit after the loop. That is the
usual way a finished build is entered: the coder completes its subtasks in one
invocation, and the next one finds the work done. So `build_report.json`
recorded `gates: null` and the build reported success having executed no test,
which kept AIFactory#1491 alive through five fixes downstream of this return.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

_CODER = _BACKEND / "agents" / "coder.py"


def _run_autonomous_agent_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(_CODER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_autonomous_agent":
            return node
    raise AssertionError("run_autonomous_agent not found")


def _gate_call_lines(fn: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(fn)
        for func in [getattr(node, "func", None)]
        if isinstance(node, ast.Call)
        and isinstance(func, ast.Name)
        and func.id == "_run_trailing_gates_if_build_complete"
    ]


def _already_complete_return_line(fn: ast.AST) -> int:
    """The `return` inside the `if is_build_complete(...)` guard."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "is_build_complete"
        ):
            returns = [n.lineno for n in ast.walk(node) if isinstance(n, ast.Return)]
            if returns:
                return min(returns)
    raise AssertionError("the is_build_complete early-return guard was not found")


def test_the_already_complete_path_runs_gates_before_returning():
    fn = _run_autonomous_agent_node()
    early_return = _already_complete_return_line(fn)
    gate_calls = _gate_call_lines(fn)

    assert gate_calls, "run_autonomous_agent never calls the trailing gates"
    before_the_return = [line for line in gate_calls if line < early_return]
    assert before_the_return, (
        "the already-complete path returns before the trailing gates: a finished "
        "build entered this way reports success having run no gate at all"
    )


def test_the_post_loop_gate_call_is_still_there():
    # The serial path still needs its own call after the loop; this test exists
    # so removing it to 'deduplicate' fails loudly.
    fn = _run_autonomous_agent_node()
    early_return = _already_complete_return_line(fn)

    assert [line for line in _gate_call_lines(fn) if line > early_return]
