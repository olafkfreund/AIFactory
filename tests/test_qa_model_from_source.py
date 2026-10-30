"""#1969: QA loop model and thinking selection reads source_spec_dir, not worktree."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import phase_config  # noqa: E402
import qa_loop  # noqa: E402


class _StopQAError(Exception):
    pass


@pytest.mark.asyncio
async def test_qa_model_comes_from_source_not_worktree_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIFACTORY_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("AIFACTORY_ROUTING_POLICY", raising=False)

    source = tmp_path / "source"
    source.mkdir()
    (source / "task_metadata.json").write_text(json.dumps({}))

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "QA_FIX_REQUEST.md").write_text("fix it\n")
    (worktree / "task_metadata.json").write_text(
        json.dumps(
            {
                "phaseModels": {"qa": "haiku", "qa_fixer": "haiku"},
                "phaseThinking": {"qa": "none", "qa_fixer": "none"},
            }
        )
    )

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    create_client_mock = MagicMock(return_value=client)
    reviewer_provider_mock = MagicMock(side_effect=_StopQAError)

    loop_globals = qa_loop.run_qa_validation_loop.__globals__
    with (
        patch.dict(
            loop_globals,
            {
                "is_build_complete": MagicMock(return_value=True),
                "is_qa_approved": MagicMock(return_value=False),
                "is_no_test_project": MagicMock(return_value=False),
                "get_task_logger": MagicMock(return_value=None),
                "emit_phase": MagicMock(),
                "create_client": create_client_mock,
                "run_qa_fixer_session": AsyncMock(return_value=("fixed", "")),
                "_create_qa_reviewer_provider": reviewer_provider_mock,
            },
        ),
        pytest.raises(_StopQAError),
    ):
        await qa_loop.run_qa_validation_loop(
            project_dir=tmp_path,
            spec_dir=worktree,
            model="sonnet",
            source_spec_dir=source,
        )

    haiku_id = phase_config.resolve_model_id("haiku")
    none_budget = phase_config.get_thinking_budget("none")

    assert create_client_mock.call_count == 1
    fixer_call = create_client_mock.call_args
    assert fixer_call.args[2] != haiku_id
    assert fixer_call.kwargs.get("max_thinking_tokens") != none_budget

    assert reviewer_provider_mock.call_count == 1
    reviewer_call = reviewer_provider_mock.call_args
    assert reviewer_call.args[3] != haiku_id
    assert reviewer_call.args[4] != none_budget
