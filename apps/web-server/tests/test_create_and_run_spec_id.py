"""Regression test for #232.

create_and_run_task used a temporary "pending-<uuid>" spec id. After the spec
orchestrator gathered requirements it renamed any "pending" directory
(rename_spec_dir_from_requirements), which desynced the board's task id from the
on-disk spec dir — the tracked task showed as "stuck" while a separate
(often doubled-slug) skeleton appeared. The fix seeds a deterministic NNN-slug
id + requirements.json up front, so no rename happens and the ids stay in sync.
"""

import asyncio
from unittest.mock import AsyncMock, patch


def test_create_and_run_uses_non_pending_spec_id(tmp_path):
    from server.routes import execution

    project_id = "proj-1"
    project_path = tmp_path / "repo"
    (project_path / ".aifactory" / "specs").mkdir(parents=True)

    # create_and_run_task takes CreateAndRunRequest, not the StartTaskRequest
    # base — it reads request.provenance (#332), which the base model lacks.
    req = execution.CreateAndRunRequest()
    with (
        patch.object(
            execution,
            "load_projects",
            return_value={project_id: {"path": str(project_path)}},
        ),
        patch.object(execution, "get_agent_service") as mock_svc,
    ):
        mock_svc.return_value.start_spec_creation = AsyncMock(return_value=None)
        result = asyncio.run(
            execution.create_and_run_task(
                project_id,
                "Add /metrics endpoint",
                "Add a /metrics endpoint returning request counts, with a test.",
                req,
            )
        )

    tid = result["task_id"]
    # 1. No transient pending id (the rename trigger).
    assert "pending" not in tid, f"create-and-run still uses a pending id: {tid}"

    # 2. The spec dir + requirements.json are seeded under that id.
    spec_id = tid.split(":", 1)[1]
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    assert (spec_dir / "requirements.json").exists(), "requirements.json was not seeded"

    # 3. The id handed to the pipeline matches the returned/tracked id (no desync).
    called = mock_svc.return_value.start_spec_creation.call_args.kwargs["task_id"]
    assert called == tid
