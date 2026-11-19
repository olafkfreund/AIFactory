"""A task with no contract still needs a toolchain.

`materialize_flake_into` writes nothing when the contract carries no
`environment`, and said nothing about it. No flake means `nix develop
path:/work#default` has nothing to enter, so the gate lane cannot supply a
toolchain and the build falls back to bare shell in a control-plane pod. A live
Kotlin task reported `gradle: exit 127`, ran no gate at all, and was returned as
APPROVED on inspection (AIFactory#1491, #1496).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_BACKEND / "core"))

from core.nix_env import infer_environment, materialize_flake_into  # noqa: E402


def _kotlin_project(root: Path) -> Path:
    """The pfactory-friends-demo shape: a gradle module below the root."""
    (root / "lanes/kotlin-core/src/main/kotlin").mkdir(parents=True)
    (root / "lanes/kotlin-core/build.gradle.kts").write_text(
        'plugins { kotlin("jvm") }\n'
    )
    (root / "lanes/kotlin-core/src/main/kotlin/Profile.kt").write_text(
        "data class Profile(val id: String)\n"
    )
    return root


def test_a_kotlin_project_infers_its_toolchain_and_verify_command(tmp_path):
    env = infer_environment(_kotlin_project(tmp_path))

    assert env is not None
    assert env["language"] == "kotlin"
    # Straight from contracts/languages/kotlin.yaml, not invented here.
    assert env["verify_commands"] == ["gradle test --no-daemon --console=plain"]
    assert env["provisioning"] == {"method": "nix", "generated": True}
    # The caller must be able to tell an inferred environment from a contract one.
    assert env["inferred"] is True


def test_the_inferred_environment_produces_a_flake_carrying_the_jvm(tmp_path):
    # The point of the whole exercise: `gradle` has to exist inside the dev
    # shell the gates enter. A flake without it leaves `gradle: exit 127`.
    root = _kotlin_project(tmp_path)

    assert materialize_flake_into(root, infer_environment(root)) is True

    flake = (root / "flake.nix").read_text()
    assert "gradle" in flake
    assert "jdk21" in flake
    assert "kotlin" in flake


def test_a_language_with_no_descriptor_is_not_guessed_at(tmp_path):
    # Better no environment than a made-up one: an unknown stack keeps the
    # previous behaviour rather than getting a toolchain nobody declared.
    (tmp_path / "main.f90").write_text("program p\nend program p\n")

    assert infer_environment(tmp_path) is None


def test_an_empty_project_infers_nothing(tmp_path):
    assert infer_environment(tmp_path) is None


# --- builtin languages (AIFactory#2142 rust, #2143 java) ---------------------
# Rust and Java have a toolchain table in nix_provisioner but no descriptor, so
# inference returned None, no flake was written, and the gate lane had no cargo
# or JDK. Pre-fix every test below that expects an environment got None.

import logging  # noqa: E402


def _flake_for(root: Path) -> str:
    env = infer_environment(root)
    assert env is not None
    assert materialize_flake_into(root, env) is True
    return (root / "flake.nix").read_text()


def _rust(root: Path) -> Path:
    (root / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    (root / "src").mkdir()
    (root / "src/lib.rs").write_text("pub fn f() {}\n")
    return root


def _maven(root: Path, release: str = "21") -> Path:
    (root / "pom.xml").write_text(
        "<project><properties>"
        f"<maven.compiler.release>{release}</maven.compiler.release>"
        "</properties></project>\n"
    )
    (root / "src/main/java").mkdir(parents=True)
    (root / "src/main/java/A.java").write_text("class A {}\n")
    return root


def test_rust_project_infers_cargo_rustc_and_clippy(tmp_path):
    root = _rust(tmp_path)
    env = infer_environment(root)

    assert env is not None
    assert env["language"] == "rust"
    assert env["verify_commands"] == ["cargo test --quiet"]
    flake = _flake_for(root)
    for attr in ("pkgs.rustc", "pkgs.cargo", "pkgs.clippy"):
        assert attr in flake


def test_maven_project_infers_jdk21_and_maven(tmp_path):
    root = _maven(tmp_path)
    env = infer_environment(root)

    assert env is not None
    assert env["language"] == "java"
    assert env["verify_commands"] == ["mvn -B -q test"]
    flake = _flake_for(root)
    assert "pkgs.jdk21" in flake
    assert "pkgs.maven" in flake


def test_nested_maven_module_is_found(tmp_path):
    (tmp_path / "service").mkdir()
    _maven(tmp_path / "service")

    env = infer_environment(tmp_path)

    assert env is not None and env["language"] == "java"


def test_gradle_java_project_adds_gradle(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("plugins { java }\n")
    (tmp_path / "src/main/java").mkdir(parents=True)
    (tmp_path / "src/main/java/A.java").write_text("class A {}\n")

    env = infer_environment(tmp_path)

    assert env is not None and env["language"] == "java"
    assert env["verify_commands"] == ["gradle test --no-daemon --console=plain"]
    assert "pkgs.gradle" in _flake_for(tmp_path)


def test_gradlew_alone_counts_as_gradle_java(tmp_path):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")

    env = infer_environment(tmp_path)

    assert env is not None and env["language"] == "java"


def test_kotlin_keeps_priority_over_java_on_a_gradle_repo(tmp_path):
    root = _kotlin_project(tmp_path)
    (root / "build.gradle").write_text("// groovy build\n")  # detector adds java

    env = infer_environment(root)

    assert env is not None and env["language"] == "kotlin"


def test_kotlin_plus_rust_flake_carries_both_toolchains(tmp_path):
    root = _rust(_kotlin_project(tmp_path))

    flake = _flake_for(root)

    for attr in ("pkgs.kotlin", "pkgs.gradle", "pkgs.rustc", "pkgs.cargo"):
        assert attr in flake


def test_maven_plus_gradle_modules_flake_has_both_build_tools(tmp_path):
    _maven(tmp_path)
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod/build.gradle").write_text("plugins { id 'java' }\n")

    flake = _flake_for(tmp_path)

    assert "pkgs.maven" in flake and "pkgs.gradle" in flake


def test_go_project_infers_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.25\n")

    env = infer_environment(tmp_path)

    assert env is not None and env["language"] == "go"
    assert env["verify_commands"] == ["go test ./..."]


def test_node_project_infers_node(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"jest"}}\n')

    env = infer_environment(tmp_path)

    assert env is not None and env["language"] == "javascript"
    assert "pkgs.nodejs_22" in _flake_for(tmp_path)


def test_rust_plus_node_flake_has_both(tmp_path):
    root = _rust(tmp_path)
    (root / "package.json").write_text('{"name":"x"}\n')

    flake = _flake_for(root)

    assert "pkgs.cargo" in flake and "pkgs.nodejs_22" in flake


def test_python_alone_infers_nothing(tmp_path):
    # Python keeps the default harness / venv path; inferring would swap it.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "app.py").write_text("x = 1\n")

    assert infer_environment(tmp_path) is None


def test_python_plus_rust_keeps_python_harness_and_adds_cargo(tmp_path):
    root = _rust(tmp_path)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "app.py").write_text("x = 1\n")

    env = infer_environment(root)

    assert env is not None and env["language"] == "python"
    flake = _flake_for(root)
    assert "pytest" in flake and "pkgs.cargo" in flake


def test_repo_owned_flake_survives_inference(tmp_path):
    root = _rust(tmp_path)
    (root / "flake.nix").write_text("# repo owned\n")

    materialize_flake_into(root, infer_environment(root))

    assert (root / "flake.nix").read_text() == "# repo owned\n"


def test_kotlin_inference_also_preserves_a_repo_flake(tmp_path):
    root = _kotlin_project(tmp_path)
    (root / "flake.nix").write_text("# repo owned\n")

    materialize_flake_into(root, infer_environment(root))

    assert (root / "flake.nix").read_text() == "# repo owned\n"


def test_pom_asking_for_a_newer_jdk_than_the_flake_ships_warns(tmp_path, caplog):
    root = _maven(tmp_path, release="25")

    with caplog.at_level(logging.WARNING):
        infer_environment(root)

    assert any(
        "25" in r.getMessage() and "jdk21" in r.getMessage() for r in caplog.records
    )


def test_kotlin_gradle_repo_does_not_buy_maven(tmp_path):
    root = _kotlin_project(tmp_path)
    (root / "build.gradle").write_text("// groovy build\n")

    assert "pkgs.maven" not in _flake_for(root)


def test_other_task_worktrees_do_not_trigger_java(tmp_path):
    (tmp_path / ".aifactory/worktrees/tasks/other").mkdir(parents=True)
    _maven(tmp_path / ".aifactory/worktrees/tasks/other")
    (tmp_path / "x.f90").write_text("program p\nend program p\n")

    assert infer_environment(tmp_path) is None
