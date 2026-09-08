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
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "run_autonomous_agent"
        ):
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


def _already_complete_guard(fn: ast.AST) -> ast.If:
    """The `if is_build_complete(...)` block that returns early."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "is_build_complete"
            and any(isinstance(n, ast.Return) for n in ast.walk(node))
        ):
            return node
    raise AssertionError("the is_build_complete early-return guard was not found")


def test_the_already_complete_path_runs_gates_before_returning():
    # The call has to be INSIDE the guard, not merely earlier in the function:
    # a refactor that moved the post-loop call up would otherwise satisfy a
    # line-number comparison while reintroducing the bug.
    guard = _already_complete_guard(_run_autonomous_agent_node())

    assert _gate_call_lines(guard), (
        "the already-complete path returns without calling the trailing gates: a "
        "finished build entered this way reports success having run no gate at all"
    )


def test_the_post_loop_gate_call_is_still_there():
    # The serial path still needs its own call after the loop; this test exists
    # so removing it to "deduplicate" fails loudly. Counted outside the guard so
    # the guard's own call cannot satisfy it.
    fn = _run_autonomous_agent_node()
    guard = _already_complete_guard(fn)
    in_guard = set(_gate_call_lines(guard))

    assert [line for line in _gate_call_lines(fn) if line not in in_guard]
