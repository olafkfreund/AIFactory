"""#1443: a Python test command in a JavaScript project is refused, not run."""

import json
import subprocess
from pathlib import Path

import pytest
from agents.gate_runner import _has_python_test_harness, detect_gates
from core.nix_env import materialize_flake_into


def _pkg(tmp_path: Path, test: str) -> Path:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": test}}))
    return tmp_path


def _names(p: Path) -> list[str]:
    return [g.name for g in detect_gates(p)]


@pytest.mark.parametrize(
    "script",
    [
        "pytest -q",
        "py.test",
        "python -m pytest",
        "python3 -m unittest",
        "npm run build && pytest",
    ],
)
def test_python_runner_in_js_project_is_a_failing_gate(tmp_path, script):
    names = _names(_pkg(tmp_path, script))
    assert "test-script-language" in names
    assert "test" not in names


def test_same_script_with_python_harness_is_a_normal_test_gate(tmp_path):
    p = _pkg(tmp_path, "pytest -q")
    (p / "pytest.ini").write_text("[pytest]\n")
    names = _names(p)
    assert "test" in names and "test-script-language" not in names


@pytest.mark.parametrize(
    "script", ["jest", "vitest run", "node --test", "mypytest-wrapper"]
)
def test_js_runner_unchanged(tmp_path, script):
    names = _names(_pkg(tmp_path, script))
    assert "test" in names and "test-script-language" not in names


def test_harness_detection(tmp_path):
    assert not _has_python_test_harness(tmp_path)
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "test_a.py").write_text("")
    assert not _has_python_test_harness(tmp_path)  # vendored, not the project's
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert _has_python_test_harness(tmp_path)


def test_harness_detection_by_test_file(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "thing_test.py").write_text("")
    assert _has_python_test_harness(tmp_path)


_GENERATED = {"provisioning": {"method": "nix", "generated": True}}


def test_unset_language_js_project_gets_node_not_pytest(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert materialize_flake_into(tmp_path, dict(_GENERATED))
    flake = (tmp_path / "flake.nix").read_text()
    assert "nodejs" in flake and "pytest" not in flake


@pytest.mark.parametrize(
    "marker",
    ["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pytest.ini"],
)
def test_unset_language_with_python_marker_keeps_python_default(tmp_path, marker):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / marker).write_text("")
    assert materialize_flake_into(tmp_path, dict(_GENERATED))
    assert "pytest" in (tmp_path / "flake.nix").read_text()


def test_unset_language_no_package_json_keeps_python_default(tmp_path):
    assert materialize_flake_into(tmp_path, dict(_GENERATED))
    assert "pytest" in (tmp_path / "flake.nix").read_text()


def test_gate_message_is_printed_literally_never_run(tmp_path):
    """The script text is agent-written: it reaches sh as $0, printed by printf,
    so a backslash, a leading -n or a command substitution is only text."""
    _pkg(tmp_path, "-n pytest -q \\t $(touch pwned)")
    gate = next(g for g in detect_gates(tmp_path) if g.name == "test-script-language")
    r = subprocess.run(gate.command, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 1
    assert "-n pytest -q" in r.stderr and "\\t" in r.stderr
    assert not (tmp_path / "pwned").exists()
