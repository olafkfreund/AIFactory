"""Issue #1771 — a monitor that raises must still record a terminal status.

The catch-all handler in ``monitor_process`` freed memory and emitted FAILED but
never wrote the plan/control status, so the board showed ``in_progress``
forever. This drives the real handler: the process wait raises.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services import task_control  # noqa: E402
from server.services.agent_service import AgentService, TaskPhase  # noqa: E402
from server.services.process_monitor import monitor_process  # noqa: E402


class _RaisingProcess:
    async def wait(self) -> int:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_monitor_raise_records_terminal_failed(tmp_path: Path) -> None:
    spec_id, task_id = "001", "proj:001"
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps(
            {
                "status": "in_progress",
                "phases": [{"name": "build", "subtasks": [{"id": "1.1"}]}],
            }
        )
    )
    task_control.write_control(spec_dir, status="in_progress", updated_by="test")

    service = AgentService()
    service._emit_progress = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "server.services.completion_orchestration.run_terminal_completion",
        new=AsyncMock(),
    ):
        await monitor_process(
            service,
            task_id,
            _RaisingProcess(),  # type: ignore[arg-type]
            tmp_path,
            spec_id,
        )

    control = task_control.read_control(spec_dir)
    assert control.get("status") == "human_review"
    assert control.get("reviewReason") == "errors"
    assert service._emit_progress.await_args.args[0].phase is TaskPhase.FAILED
