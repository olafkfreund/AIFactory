"""A build that dies before writing its plan file must still report failure (#1430).

The incident: the cockpit showed LIVE AGENTS: #561 PLANNING for a build that had
failed 50 minutes earlier. Not a rendering bug -- AIFactory's own API reported
``status=in_progress phase=planning`` while its own logs already said::

    08:58:05 [build_backend] reaped stranded build ...:156-build-a-playable-...:
             k8s Job factory/factory-aifactory-... reported failed
    09:04:50 [AgentService._update_plan_status] CALLED status=failed

Task status lives in implementation_plan.json. This build failed before writing
one, so the terminal update hit ``if not plan_file.exists(): return`` and was
dropped -- leaving the task reporting its last phase forever, and the dispatching
card stuck at "dispatched" so the sequence could neither advance nor fail.

The earlier a build failed, the more certain it was to be recorded as running.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services.agent_service import (  # noqa: E402
    _TERMINAL_PLAN_STATUSES,
    AgentService,
)


def _spec_dir(project: Path, spec_id: str) -> Path:
    return project / ".aifactory" / "specs" / spec_id


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose spec dir exists but has NO plan file -- the shape a build
    leaves behind when it dies during planning."""
    d = _spec_dir(tmp_path, "156-build")
    d.mkdir(parents=True)
    (d / "requirements.json").write_text("{}")
    return tmp_path


@pytest.mark.asyncio
async def test_a_terminal_status_is_recorded_when_no_plan_was_written(
    project: Path,
) -> None:
    """The headline: failure must be recorded even with nothing to record it in."""
    svc = AgentService.__new__(AgentService)

    await AgentService._update_plan_status(
        svc, project, "156-build", "failed", "t1", emit_events=False
    )

    plan = json.loads(
        (_spec_dir(project, "156-build") / "implementation_plan.json").read_text()
    )
    assert plan["status"] == "failed"


@pytest.mark.asyncio
async def test_completed_is_terminal_too(project: Path) -> None:
    svc = AgentService.__new__(AgentService)

    await AgentService._update_plan_status(
        svc, project, "156-build", "completed", "t1", emit_events=False
    )

    plan = json.loads(
        (_spec_dir(project, "156-build") / "implementation_plan.json").read_text()
    )
    assert plan["status"] == "completed"


@pytest.mark.asyncio
async def test_a_mid_run_status_does_not_invent_a_plan(project: Path) -> None:
    """Only a terminal status justifies creating a file nobody wrote. A mid-run
    status has a live process that will write one properly, and fabricating a
    plan under it would replace real content moments later."""
    svc = AgentService.__new__(AgentService)

    await AgentService._update_plan_status(
        svc, project, "156-build", "human_review", "t1", emit_events=False
    )

    assert not (_spec_dir(project, "156-build") / "implementation_plan.json").exists()


def test_human_review_is_not_treated_as_terminal() -> None:
    """It is a checkpoint. A task reaching it has a plan by definition."""
    assert "human_review" not in _TERMINAL_PLAN_STATUSES
    assert _TERMINAL_PLAN_STATUSES == {"completed", "failed"}


@pytest.mark.asyncio
async def test_an_existing_plan_is_never_replaced(project: Path) -> None:
    """#1081's rule holds: a plan that EXISTS must not be clobbered by a status
    write. The fallback is for an ABSENT file -- there is nothing to lose when
    none was ever written, which is not true once one has been."""
    plan_file = _spec_dir(project, "156-build") / "implementation_plan.json"
    plan_file.write_text(
        json.dumps({"status": "in_progress", "phases": [{"phase": 1}]})
    )
    svc = AgentService.__new__(AgentService)

    await AgentService._update_plan_status(
        svc, project, "156-build", "failed", "t1", emit_events=False
    )

    plan = json.loads(plan_file.read_text())
    assert plan.get("phases"), "the real plan content must survive a status write"
    assert plan.get("recorded_by") != "terminal-status-fallback"
