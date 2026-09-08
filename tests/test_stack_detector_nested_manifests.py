"""A build tool below the repo root is still this project's build tool.

`ConfigParser.file_exists` with a plain name only tests `project_dir / name`,
so every manifest check in `detect_package_managers` missed a repo whose builds
live under a subdirectory — lanes/kotlin-core, services/api, packages/web. The
language rules glob recursively ("**/*.kt"), so the result was a project
detected as Kotlin with no build tool, and `gradle test` refused with "not in
the allowed commands for this project" (AIFactory#1491): the command allowlist
is assembled from the detected package managers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from project.command_registry import (  # noqa: E402
    LANGUAGE_COMMANDS,
    PACKAGE_MANAGER_COMMANDS,
)
from project.stack_detector import StackDetector  # noqa: E402


def _allowed(project_dir: Path) -> tuple[list[str], set[str]]:
    """The detected package managers and the commands they unlock."""
    stack = StackDetector(project_dir).detect_all()
    commands: set[str] = set()
    for lang in stack.languages:
        commands |= LANGUAGE_COMMANDS.get(lang, set())
    for pm in stack.package_managers:
        commands |= PACKAGE_MANAGER_COMMANDS.get(pm, set())
    return stack.package_managers, commands


def test_a_gradle_module_below_the_root_unlocks_gradle(tmp_path):
    # The pfactory-friends-demo layout: build files under lanes/, not at root.
    (tmp_path / "lanes/kotlin-core/src/main/kotlin").mkdir(parents=True)
    (tmp_path / "lanes/kotlin-core/build.gradle.kts").write_text(
        'plugins { kotlin("jvm") }\n'
    )
    (tmp_path / "lanes/kotlin-core/src/main/kotlin/Profile.kt").write_text(
        "data class P(val i: String)\n"
    )

    managers, commands = _allowed(tmp_path)

    assert "gradle" in managers
    assert {"gradle", "gradlew"} <= commands


def test_a_manifest_at_the_root_is_unaffected(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("plugins {}\n")
    (tmp_path / "Main.kt").write_text("fun main() {}\n")

    managers, commands = _allowed(tmp_path)

    assert "gradle" in managers
    assert "gradle" in commands


def test_a_dependencys_manifest_does_not_count_as_this_project(tmp_path):
    # A package-lock.json under node_modules belongs to a dependency. Recursing
    # without this guard would declare every repo an npm project.
    (tmp_path / "node_modules/leftpad").mkdir(parents=True)
    (tmp_path / "node_modules/leftpad/package-lock.json").write_text("{}\n")
    (tmp_path / "app.py").write_text("print('x')\n")

    managers, _ = _allowed(tmp_path)

    assert "npm" not in managers


def test_the_search_is_bounded(tmp_path):
    # Deeper than _MANIFEST_MAX_DEPTH: not found, rather than walking a whole
    # tree on every detection.
    deep = tmp_path.joinpath(*(["a"] * 8))
    deep.mkdir(parents=True)
    (deep / "Cargo.toml").write_text("[package]\n")

    managers, _ = _allowed(tmp_path)

    assert "cargo" not in managers
