"""RunGates reads every run gate once, before the first agent session (#1983)."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from phase_config import (  # noqa: E402
    get_phase_model,
    get_phase_thinking_budget,
    get_thinking_budget,
)
from review.state import ReviewState  # noqa: E402
from run_gates import RunGates  # noqa: E402

PLAN = {"feature": "a", "phases": [{"phase": 1, "subtasks": [{"id": "1"}]}]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AIFACTORY_FALLBACK_MODEL", "AIFACTORY_SOLO_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AIFACTORY_MODEL_REGISTRY_ENFORCE", "off")


def _spec(tmp_path: Path, name: str = "spec", **meta: object) -> Path:
    spec = tmp_path / name
    spec.mkdir()
    (spec / "implementation_plan.json").write_text(json.dumps(PLAN))
    if meta:
        (spec / "task_metadata.json").write_text(json.dumps(meta))
    return spec


def test_capture_reads_source_dir_not_worktree_copy(tmp_path: Path) -> None:
    source = _spec(tmp_path, "source", soloMode=True)
    worktree = _spec(tmp_path, "worktree", requireReviewBeforeCoding=True)
    gates = RunGates.capture(worktree, source, None)
    assert gates.solo is True
    assert gates.requires_review is False


def test_metadata_empty_when_absent_and_frozen_after_capture(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    gates = RunGates.capture(spec, None, None)
    assert gates.metadata == {}
    before = (gates.model("qa"), gates.thinking_budget("qa"))
    (spec / "task_metadata.json").write_text(
        json.dumps({"pinnedModel": "haiku", "thinkingLevel": "none"})
    )
    assert (gates.model("qa"), gates.thinking_budget("qa")) == before
    assert gates.thinking_budget("qa") != get_thinking_budget("none")


def test_forged_approval_after_capture_is_ignored(tmp_path: Path) -> None:
    spec = _spec(tmp_path, requireReviewBeforeCoding=True)
    ReviewState().approve(spec)
    gates = RunGates.capture(spec, None, None)
    assert gates.approval_valid(spec)

    plan = {**PLAN, "feature": "changed"}
    (spec / "implementation_plan.json").write_text(json.dumps(plan))
    assert not gates.approval_valid(spec)
    ReviewState().approve(spec)  # forged: hashes of the changed plan
    assert not gates.approval_valid(spec)


def test_forged_approval_does_not_validate_unapproved_capture(tmp_path: Path) -> None:
    spec = _spec(tmp_path, requireReviewBeforeCoding=True)
    gates = RunGates.capture(spec, None, None)
    ReviewState().approve(spec)
    assert not gates.approval_valid(spec)


def test_frozen(tmp_path: Path) -> None:
    gates = RunGates.capture(_spec(tmp_path), None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        gates.solo = True  # type: ignore[misc]


def test_explicit_metadata_wins_over_the_file(tmp_path: Path) -> None:
    spec = _spec(tmp_path, pinnedModel="haiku", thinkingLevel="none")
    assert get_phase_model(spec, "qa", metadata={}) != get_phase_model(spec, "qa")
    assert get_phase_thinking_budget(spec, "qa", metadata={}) != (
        get_thinking_budget("none")
    )
