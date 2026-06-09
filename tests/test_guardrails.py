"""Tests for the Act-loop anti-loop guardrail (#474)."""

from __future__ import annotations

from agents.guardrails import (
    Decision,
    ToolCallGuardrailController,
    signature_for,
)


def _fail(ctrl, tool, args, n):
    last = None
    for _ in range(n):
        ctrl.before_call(tool, args)
        last = ctrl.after_call(tool, args, ok=False)
    return last


def test_signature_stable_and_args_sensitive():
    a = signature_for("Bash", {"command": "ls"})
    b = signature_for("Bash", {"command": "ls"})
    c = signature_for("Bash", {"command": "pwd"})
    assert a == b and a.key() == b.key()
    assert a != c


def test_repeated_exact_failure_blocks_after_threshold():
    ctrl = ToolCallGuardrailController(repeated_exact_failure=5, same_tool_failure=99)
    args = {"command": "false"}
    _fail(ctrl, "Bash", args, 5)
    v = ctrl.before_call("Bash", args)
    assert v.decision is Decision.BLOCK
    assert v.policy == "repeated_exact_failure"
    # a different call of the same tool is still allowed
    assert ctrl.before_call("Bash", {"command": "echo hi"}).decision is Decision.ALLOW


def test_success_resets_the_failure_streak():
    ctrl = ToolCallGuardrailController(repeated_exact_failure=3, same_tool_failure=99)
    args = {"command": "flaky"}
    _fail(ctrl, "Bash", args, 2)
    ctrl.before_call("Bash", args)
    ctrl.after_call("Bash", args, ok=True)  # success resets
    _fail(ctrl, "Bash", args, 2)
    assert (
        ctrl.before_call("Bash", args).decision is Decision.ALLOW
    )  # only 2 since reset


def test_same_tool_failure_halts():
    ctrl = ToolCallGuardrailController(repeated_exact_failure=99, same_tool_failure=8)
    # 8 failures across *different* args of one tool → halt
    for i in range(8):
        ctrl.before_call("Edit", {"file": f"f{i}.py"})
        v = ctrl.after_call("Edit", {"file": f"f{i}.py"}, ok=False)
    assert v.decision is Decision.HALT
    assert v.policy == "same_tool_failure"
    assert ctrl.halt_reason and "Edit" in ctrl.halt_reason
    # once halted, every subsequent before_call returns HALT
    assert ctrl.before_call("Read", {"file": "x"}).decision is Decision.HALT


def test_idempotent_no_progress_blocks_on_repeat_identical_result():
    ctrl = ToolCallGuardrailController(idempotent_no_progress=5, same_tool_failure=99)
    args = {"file_path": "a.py"}
    for _ in range(5):
        ctrl.before_call("Read", args)
        ctrl.after_call("Read", args, ok=True, result="same contents")
    v = ctrl.before_call("Read", args)
    assert v.decision is Decision.BLOCK
    assert v.policy == "idempotent_no_progress"


def test_idempotent_changing_result_is_progress():
    ctrl = ToolCallGuardrailController(idempotent_no_progress=3, same_tool_failure=99)
    args = {"file_path": "a.py"}
    for i in range(6):
        ctrl.before_call("Read", args)
        ctrl.after_call(
            "Read", args, ok=True, result=f"contents v{i}"
        )  # changes each time
    assert ctrl.before_call("Read", args).decision is Decision.ALLOW  # never stalls


def test_bash_is_not_treated_as_idempotent():
    # A repeating Bash command with identical output must NOT trip no-progress
    # (e.g. `pytest` legitimately re-runs); only the read-only tools do.
    ctrl = ToolCallGuardrailController(idempotent_no_progress=3, same_tool_failure=99)
    args = {"command": "pytest"}
    for _ in range(6):
        ctrl.before_call("Bash", args)
        ctrl.after_call("Bash", args, ok=True, result="2 passed")
    assert ctrl.before_call("Bash", args).decision is Decision.ALLOW


def test_thresholds_from_env(monkeypatch):
    monkeypatch.setenv("AIFACTORY_GUARDRAIL_REPEAT_FAIL", "2")
    ctrl = ToolCallGuardrailController()
    assert ctrl.repeated_exact_failure == 2
    args = {"command": "false"}
    _fail(ctrl, "Bash", args, 2)
    assert ctrl.before_call("Bash", args).decision is Decision.BLOCK
