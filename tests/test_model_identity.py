"""Identity capture must include every session that can write task code."""

import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents.model_identity import clear_identities, read_identities, record_identity

from tests.test_solo_mode import _make_spec, _run_one_turn


def _append_many(spec_dir: Path, worker: int) -> None:
    for number in range(12):
        record_identity(
            spec_dir,
            phase="coding",
            subtask_id=f"{worker}-{number}",
            model="claude-sonnet-4-5",
            provider="claude",
        )


def test_identity_without_usage_payload(tmp_path: Path) -> None:
    record_identity(
        tmp_path,
        phase="coding",
        subtask_id="1",
        model="claude-sonnet-4-5",
        provider="claude",
    )
    assert read_identities(tmp_path) == [
        {
            "phase": "coding",
            "subtask_id": "1",
            "model": "claude-sonnet-4-5",
            "provider": "claude",
            "served_model": None,
        }
    ]
    clear_identities(tmp_path)
    assert read_identities(tmp_path) == []


@pytest.mark.asyncio
async def test_solo_first_session_is_coding_without_later_session(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AIFACTORY_SOLO_MODE", "true")
    spec_dir = _make_spec(tmp_path)
    await _run_one_turn(spec_dir, tmp_path)
    assert [entry["phase"] for entry in read_identities(spec_dir)] == ["coding"]


@pytest.mark.asyncio
async def test_subtask_override_and_worktree_write_to_source(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")
    worktree = _make_spec(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    subtask = {"id": "S1", "description": "Do it", "model": "claude-opus-4-5"}
    await _run_one_turn(
        worktree,
        tmp_path,
        source_spec_dir=source,
        next_subtask=subtask,
        first_run=False,
    )
    assert read_identities(worktree) == []
    assert [
        (entry["subtask_id"], entry["model"]) for entry in read_identities(source)
    ] == [("S1", "claude-opus-4-5")]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_is_devnull", [False, True])
async def test_both_fixer_sites_recorded(
    tmp_path: Path, source_is_devnull: bool
) -> None:
    from qa import loop

    spec = tmp_path / "worktree"
    spec.mkdir()
    (spec / "QA_FIX_REQUEST.md").write_text("Fix it")
    source = Path(os.devnull) if source_is_devnull else tmp_path / "source"
    if not source_is_devnull:
        source.mkdir()
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    review_cycle = MagicMock(cycle_id=1, state=MagicMock(value="requested"))
    gates = MagicMock(requires_review=False)
    gates.model.side_effect = lambda phase: f"claude-{phase}"
    gates.thinking_budget.return_value = None
    with (
        patch.object(loop, "is_build_complete", return_value=True),
        patch.object(loop, "is_qa_approved", return_value=False),
        patch.object(loop, "get_task_logger", return_value=None),
        patch.object(loop, "is_no_test_project", return_value=False),
        patch.object(loop, "get_qa_iteration_count", return_value=0),
        patch.object(loop, "infer_provider_from_model", return_value="claude"),
        patch.object(loop, "create_client", return_value=client),
        patch.object(loop, "_create_qa_reviewer_provider", return_value=client),
        patch.object(
            loop,
            "run_qa_agent_session",
            new=AsyncMock(side_effect=[("rejected", "issues"), ("approved", "ok")]),
        ),
        patch.object(
            loop,
            "run_qa_fixer_session",
            new=AsyncMock(return_value=("complete", "fixed")),
        ),
        patch.object(loop, "get_qa_signoff_status", return_value={"issues_found": []}),
        patch.object(loop, "record_iteration"),
        patch.object(loop, "get_iteration_history", return_value=[]),
        patch.object(loop, "has_recurring_issues", return_value=(False, [])),
        patch.object(loop, "request_review", return_value=review_cycle),
        patch.object(loop, "record_started"),
        patch.object(loop, "resolve_review"),
        patch.object(loop, "check_git_metadata"),
    ):
        assert await loop.run_qa_validation_loop(
            tmp_path, spec, "claude-sonnet-4-5", source_spec_dir=source, gates=gates
        )
    destination = spec if source_is_devnull else source
    assert [entry["phase"] for entry in read_identities(destination)] == [
        "qa_fixer",
        "qa_fixer",
    ]
    if source_is_devnull:
        assert not (tmp_path / "model_identities.json").exists()


def test_concurrent_appends_lose_nothing(tmp_path: Path) -> None:
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_append_many, [tmp_path] * 4, range(4)))
    entries = read_identities(tmp_path)
    assert len(entries) == 48
    assert len({entry["subtask_id"] for entry in entries}) == 48
    assert isinstance(
        json.loads((tmp_path / "model_identities.json").read_text()), list
    )
