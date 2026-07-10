"""Tests for POST /api/tasks/{task_id}/handoff-tfactory (PARR source-aware verify).

The endpoint reuses the existing handoff machinery (build_ingest_payload pushes
the build branch + assembles the git_url/source_branch/contract payload;
send_handoff POSTs it to TFactory), but is callable on demand so a build parked
at human_review can be verified against its ACTUAL pushed code instead of a
text-only (hollow) spec-ingest.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


def _setup_spec(tmp_path):
    project_id = "proj-1"
    spec_id = "042-go-hello"
    project_path = tmp_path / "repo"
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    return project_id, spec_id, project_path, spec_dir


def test_handoff_pushes_and_hands_off_source_aware(tmp_path):
    from server.routes import execution

    project_id, spec_id, project_path, spec_dir = _setup_spec(tmp_path)
    task_id = f"{project_id}:{spec_id}"
    payload = {
        "project_id": "tf-proj",
        "spec_id": spec_id,
        "spec_text": "## Acceptance Criteria\n- AC#1",
        "git_url": "https://github.com/olafkfreund/aifactory-demo",
        "source_branch": "bench/go-hello",
        "contract": {"tfactory": {"lanes": ["unit"]}},
    }
    with (
        patch.object(
            execution,
            "load_projects",
            return_value={project_id: {"path": str(project_path)}},
        ),
        patch(
            "pfactory.tfactory_client.build_ingest_payload", return_value=payload
        ) as mock_build,
        patch(
            "pfactory.tfactory_client.send_handoff",
            new=AsyncMock(return_value={"sent": True, "status": 200}),
        ) as mock_send,
    ):
        result = asyncio.run(execution.handoff_to_tfactory(task_id, _access={}))

    # The build branch was pushed + payload assembled (build_ingest_payload) and
    # POSTed to TFactory (send_handoff).
    mock_build.assert_called_once()
    mock_send.assert_awaited_once_with(payload)
    # Caller gets the poll coordinates + the pushed branch.
    assert result["sent"] is True
    assert result["tfactory_spec_id"] == spec_id
    assert result["tfactory_project_id"] == "tf-proj"
    assert result["source_branch"] == "bench/go-hello"
    assert result["git_url"].endswith("aifactory-demo")
    # A local marker records the outcome.
    assert json.loads((spec_dir / "tfactory_handoff.json").read_text())["sent"] is True


def test_handoff_bad_task_id_400(tmp_path):
    from fastapi import HTTPException
    from server.routes import execution

    with pytest.raises(HTTPException) as ei:
        asyncio.run(execution.handoff_to_tfactory("no-colon", _access={}))
    assert ei.value.status_code == 400


def test_handoff_unknown_spec_404(tmp_path):
    from fastapi import HTTPException
    from server.routes import execution

    project_path = tmp_path / "repo"
    (project_path / ".aifactory" / "specs").mkdir(parents=True)
    with patch.object(
        execution, "load_projects", return_value={"proj-1": {"path": str(project_path)}}
    ):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(execution.handoff_to_tfactory("proj-1:missing", _access={}))
    assert ei.value.status_code == 404
