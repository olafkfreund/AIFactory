"""Java projects produced no gate, so QA could never be approved (AIFactory#2143).

`detect_gates` had Cargo and Go but no Maven or Gradle-for-Java gate, so a
Maven build recorded no gate evidence and `update_qa_status(approved)` was
refused (#1496); QA then hand-wrote `qa_signoff`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "core"))

from agents.gate_runner import detect_gates  # noqa: E402


def _by_name(root: Path) -> dict:
    return {g.name: g for g in detect_gates(root)}


def _pom(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "pom.xml").write_text("<project/>\n")
    (d / "A.java").write_text("class A {}\n")


def test_maven_root_gets_a_maven_gate(tmp_path):
    _pom(tmp_path)

    gate = _by_name(tmp_path)["maven-test"]

    assert gate.command == ["mvn", "-B", "-q", "test"]
    assert gate.cwd == tmp_path


def test_nested_pom_gate_runs_in_the_module_even_with_a_root_gradle_file(tmp_path):
    (tmp_path / "build.gradle").write_text("// root\n")
    _pom(tmp_path / "service")

    gates = _by_name(tmp_path)

    nested = next(g for n, g in gates.items() if n.startswith("maven-test"))
    assert nested.cwd == tmp_path / "service"
    assert nested.command[0] == "mvn"


def test_gradle_java_project_gets_a_gradle_gate(tmp_path):
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    (tmp_path / "A.java").write_text("class A {}\n")

    gate = _by_name(tmp_path)["gradle-test"]

    assert gate.command == ["gradle", "test", "--no-daemon", "--console=plain"]
    assert gate.cwd == tmp_path


def test_kotlin_module_is_not_doubled_by_a_gradle_gate(tmp_path):
    mod = tmp_path / "lanes/kotlin-core"
    (mod / "src/main/kotlin").mkdir(parents=True)
    (mod / "build.gradle.kts").write_text('plugins { kotlin("jvm") }\n')
    (mod / "src/main/kotlin/P.kt").write_text("class P\n")

    names = _by_name(tmp_path)

    assert "kotlin-unit" in names
    assert not any(n.startswith("gradle-test") for n in names)


def test_separate_java_gradle_module_still_gets_its_own_gate(tmp_path):
    kt = tmp_path / "a-kotlin"
    (kt / "src/main/kotlin").mkdir(parents=True)
    (kt / "build.gradle.kts").write_text('plugins { kotlin("jvm") }\n')
    (kt / "src/main/kotlin/P.kt").write_text("class P\n")
    jv = tmp_path / "b-java"
    jv.mkdir()
    (jv / "build.gradle").write_text("plugins { id 'java' }\n")
    (jv / "A.java").write_text("class A {}\n")

    gates = detect_gates(tmp_path)

    assert any(g.name.startswith("gradle-test") and g.cwd == jv for g in gates)
    assert any(g.name == "kotlin-unit" and g.cwd == kt for g in gates)


def test_no_java_markers_no_java_gate(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")

    names = _by_name(tmp_path)

    assert not any(n.startswith(("maven", "gradle")) for n in names)
