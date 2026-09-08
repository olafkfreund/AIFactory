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
