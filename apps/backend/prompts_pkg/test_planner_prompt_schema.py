"""The generated planner prompt must carry the real plan schema (#920).

Before #920 the planner load pointed at a directory that did not exist, so
every build silently fell back to a one-sentence prompt with no schema. These
tests fail loudly if that path breaks again.
"""

from __future__ import annotations

from pathlib import Path

import prompts_pkg.prompts as prompts_mod
import pytest
from prompts_pkg.prompt_generator import generate_planner_prompt

# Load-bearing tokens the runner depends on and the model can only emit if the
# real schema was shown to it. See apps/backend/prompts/planner.md.
_LOAD_BEARING = ("parallel_safe", "files_to_create", "files_to_modify", "depends_on")


def test_generated_planner_prompt_contains_schema(tmp_path: Path) -> None:
    prompt = generate_planner_prompt(tmp_path, tmp_path)
    for token in _LOAD_BEARING:
        assert token in prompt, f"planner prompt is missing schema token {token!r}"


def test_planner_load_hard_fails_when_path_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the loader at a bogus dir; the fix must raise, never degrade to a
    # schema-less fallback.
    monkeypatch.setattr(prompts_mod, "PROMPTS_DIR", tmp_path / "does-not-exist")
    with pytest.raises(FileNotFoundError):
        generate_planner_prompt(tmp_path, tmp_path)
