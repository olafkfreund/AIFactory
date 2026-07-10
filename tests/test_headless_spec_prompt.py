"""Regression tests for the headless spec-prompt hang.

Background: a create-and-run task could hang at 0% in "planning" forever.
The build runner (`cli.main`) called `print_specs_list(project_dir)` with the
default `auto_create=True` when a requested spec wasn't found. With no specs
present, `print_specs_list` dropped into an interactive QUICK START prompt and
called `input("> ")`. Under `agent_service` the process has no controlling TTY
and an open stdin pipe that never sends EOF, so `input()` blocked indefinitely
(the `except EOFError` never fired) — the task sat at 0% with no progress.

These tests pin the two fixes:
  1. `print_specs_list` never calls `input()` in a non-interactive context.
  2. it still returns promptly (no block) and emits the manual instructions.
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from cli.spec_commands import print_specs_list  # noqa: E402


@pytest.fixture
def empty_project(tmp_path):
    """A project dir with no specs."""
    return tmp_path


def test_no_input_when_stdin_not_a_tty(empty_project, capsys):
    """Headless run (no TTY) must not call input() — it would block forever."""
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=False),
        mock.patch(
            "builtins.input", side_effect=AssertionError("input() called headlessly")
        ),
    ):
        # Must return, not raise — i.e. input() is never reached.
        print_specs_list(empty_project, auto_create=True)

    out = capsys.readouterr().out
    assert "No specs found." in out
    # The QUICK START interactive prompt must NOT have been printed.
    assert "QUICK START" not in out


def test_no_input_when_ci_env_set(empty_project, monkeypatch, capsys):
    """CI=true (set by agent_service) also suppresses the interactive prompt."""
    monkeypatch.setenv("CI", "true")
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch(
            "builtins.input", side_effect=AssertionError("input() called under CI")
        ),
    ):
        print_specs_list(empty_project, auto_create=True)

    assert "QUICK START" not in capsys.readouterr().out


def test_no_input_when_claude_cli_entrypoint(empty_project, monkeypatch, capsys):
    """CLAUDE_CODE_ENTRYPOINT=cli (set by agent_service) also suppresses it."""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    with (
        mock.patch.object(sys.stdin, "isatty", return_value=True),
        mock.patch(
            "builtins.input",
            side_effect=AssertionError("input() called via cli entrypoint"),
        ),
    ):
        print_specs_list(empty_project, auto_create=True)

    assert "QUICK START" not in capsys.readouterr().out


def test_auto_create_false_never_prompts(empty_project, capsys):
    """The build runner passes auto_create=False on a missing spec."""
    with mock.patch(
        "builtins.input",
        side_effect=AssertionError("input() called with auto_create=False"),
    ):
        print_specs_list(empty_project, auto_create=False)

    out = capsys.readouterr().out
    assert "No specs found." in out
    assert "QUICK START" not in out
