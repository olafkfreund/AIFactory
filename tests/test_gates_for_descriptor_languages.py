"""A language the gate detector never heard of produced no gate at all.

`detect_gates` knew Python, Node/TypeScript, Rust and Go. A Kotlin project
therefore yielded an empty gate list, the gate step ran nothing, and the build
reported success having executed no test — which is how a defect that
inspection cannot catch reached a merged PR (AIFactory#1491, #1496).

Every check was also root-only, so a repo whose build lives under lanes/ or
services/ produced no gates even in a language the detector did know.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "core"))

from agents.gate_runner import (  # noqa: E402
    Gate,
    _command_in_module,
    detect_gates,
)
from cli.build_commands import _trailing_gate_evidence  # noqa: E402


def _kotlin_module(root: Path) -> Path:
    (root / "lanes/kotlin-core/src/main/kotlin").mkdir(parents=True)
    (root / "lanes/kotlin-core/build.gradle.kts").write_text(
        'plugins { kotlin("jvm") }\n'
    )
    (root / "lanes/kotlin-core/src/main/kotlin/Profile.kt").write_text(
        "data class Profile(val id: String)\n"
    )
    return root


def test_a_kotlin_project_gets_its_declared_unit_gate(tmp_path):
    gates = detect_gates(_kotlin_module(tmp_path))

    names = [g.name for g in gates]
    assert "kotlin-unit" in names, names
    gate = next(g for g in gates if g.name == "kotlin-unit")
    # Straight from contracts/languages/kotlin.yaml, not hardcoded here.
    assert gate.command[:2] == ["gradle", "test"]


def test_the_gate_runs_where_the_build_file_is(tmp_path):
    # `gradle test` at the repo root finds no build to run. The gate has to
    # carry the module directory.
    gates = detect_gates(_kotlin_module(tmp_path))

    gate = next(g for g in gates if g.name == "kotlin-unit")
    assert gate.cwd == tmp_path / "lanes/kotlin-core"


def test_an_unknown_language_still_yields_nothing(tmp_path):
    (tmp_path / "main.f90").write_text("program p\nend program p\n")

    assert detect_gates(tmp_path) == []


def test_gate_evidence_is_none_when_the_step_never_ran(tmp_path):
    assert _trailing_gate_evidence(tmp_path) is None


def test_gate_evidence_reports_an_empty_gate_run(tmp_path):
    # The sentence the gate step writes when it found nothing to run. A
    # pre-approval must be able to tell this from a real summary.
    (tmp_path / ".trailing_gates_done").write_text(f"no gates detected in {tmp_path}\n")

    evidence = _trailing_gate_evidence(tmp_path)

    assert evidence is not None
    assert evidence.startswith("no gates detected")


def test_a_root_package_json_does_not_capture_the_kotlin_gate(tmp_path):
    # The polyglot trap: markers were one shared list, so any generic build file
    # at the root won the "nearest module" race and `gradle test` would have run
    # in a directory with no Gradle build. Markers are per-language now.
    (tmp_path / "package.json").write_text('{"name": "root", "scripts": {}}\n')
    _kotlin_module(tmp_path)

    gate = next(g for g in detect_gates(tmp_path) if g.name == "kotlin-unit")

    assert gate.cwd == tmp_path / "lanes/kotlin-core"


def test_a_language_with_no_build_file_gets_no_gate(tmp_path):
    # Kotlin sources but no Gradle build anywhere: running `gradle test` from the
    # root would fail for a reason unrelated to the code under test.
    (tmp_path / "src").mkdir()
    (tmp_path / "src/Main.kt").write_text("fun main() {}\n")

    assert [g.name for g in detect_gates(tmp_path) if g.name == "kotlin-unit"] == []


def test_gate_evidence_survives_a_corrupt_marker(tmp_path):
    # Unreadable evidence is no evidence — but it must not crash finalization.
    (tmp_path / ".trailing_gates_done").write_bytes(b"\xff\xfe\x00binary")

    assert _trailing_gate_evidence(tmp_path) is None


def test_a_module_gate_is_entered_by_the_command_not_the_mount(tmp_path):
    # The runner's cwd must stay the worktree root: the Nix Job mounts it at
    # /work and reads flake.nix from there. Handing it the module mounted the
    # module instead, and nix reported "flake.nix does not exist" with a store
    # hash that never moved however the root was edited.
    gate = Gate("kotlin-unit", ["gradle", "test"], cwd=tmp_path / "lanes/kotlin-core")

    argv = _command_in_module(gate, tmp_path)

    assert argv[:2] == ["bash", "-c"]
    assert argv[2] == "cd lanes/kotlin-core && gradle test"


def test_a_root_gate_is_left_alone(tmp_path):
    gate = Gate("pytest", ["pytest", "-q"], cwd=tmp_path)

    assert _command_in_module(gate, tmp_path) == ["pytest", "-q"]


def test_a_gate_with_no_cwd_is_left_alone(tmp_path):
    gate = Gate("pytest", ["pytest", "-q"])

    assert _command_in_module(gate, tmp_path) == ["pytest", "-q"]


def test_a_module_outside_the_tree_is_left_alone(tmp_path):
    gate = Gate("odd", ["true"], cwd=tmp_path.parent / "elsewhere")

    assert _command_in_module(gate, tmp_path) == ["true"]
