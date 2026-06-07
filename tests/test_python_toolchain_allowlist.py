#!/usr/bin/env python3
"""
Regression: a Python project always allowlists its verification toolchain
=========================================================================

Stack detection runs once at build start and is cached in
.aifactory-security.json. For a from-scratch build the scaffold subtask adds the
dev toolchain (uv/pytest/ruff/mypy) *after* the scan, so detection-based
allowlisting never picks them up — the coder and QA then flail on
"Command 'uv' is not in the allowed commands for this project" (observed live in
benchmark 004, both coding and validation phases). Pinning the toolchain at the
language level guarantees any Python build can run its own verification.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from project.command_registry import LANGUAGE_COMMANDS  # noqa: E402

REQUIRED = {"uv", "uvx", "pytest", "py.test", "ruff", "mypy"}


@pytest.mark.parametrize("cmd", sorted(REQUIRED))
def test_python_toolchain_command_is_allowlisted(cmd):
    assert cmd in LANGUAGE_COMMANDS["python"], (
        f"{cmd!r} must be in the python language allowlist so Python builds can "
        f"verify themselves regardless of stack-detection timing"
    )


def test_python_still_has_interpreter_and_pip():
    py = LANGUAGE_COMMANDS["python"]
    assert {"python", "python3", "pip"} <= py


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
