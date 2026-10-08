"""#1619 — ``recover`` must refuse while that task's build is still running.

``POST /tasks/{task_id}/recover`` resets the task's record and, with
``autoRestart``, re-dispatches it. It does **not** delete the Kubernetes Job.
So recovering a live build leaves the Job writing against a reset task and can
put a second Job on the same task.

That is not hypothetical: the cockpit offered the Recover button over healthy
builds for their entire duration, because ``is_running`` could not see a kubejob
(fixed in the same change). The guard is written against the same predicate the
card reads, so the refusal and the badge cannot disagree.

The route is called directly rather than through the app: the guard is a branch
in the route, and driving auth + project loading + spec dirs through a
TestClient would test the harness, not the branch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.routes import execution  # noqa: E402

TASK = "proj-uuid:022-some-spec"


class _Agent:
    def __init__(self, running: bool) -> None:
        self._running = running
        self.running_tasks: dict[str, Any] = {}

    def is_running(self, _task_id: str) -> bool:
        return self._running


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project whose spec dir exists, so the route reaches the guard."""
    spec_dir = tmp_path / "proj" / ".aifactory" / "specs" / "022-some-spec"
    spec_dir.mkdir(parents=True)
    monkeypatch.setattr(
        execution,
        "load_projects",
        lambda: {"proj-uuid": {"path": str(tmp_path / "proj")}},
    )


@pytest.mark.asyncio
async def test_recover_refuses_while_the_build_runs(
    project: None, monkeypatch: pytest.MonkeyPatch
):
    """THE regression: recovering a live build must be a 409, not a reset."""
    monkeypatch.setattr(execution, "get_agent_service", lambda: _Agent(running=True))

    with pytest.raises(HTTPException) as caught:
        await execution.recover_task(
            TASK, execution.RecoverTaskRequest(), _access={"role": "member"}
        )

    assert caught.value.status_code == 409
    assert "still running" in str(caught.value.detail)


@pytest.mark.asyncio
async def test_force_overrides_the_refusal(
    project: None, monkeypatch: pytest.MonkeyPatch
):
    """``force=true`` is the deliberate escape hatch, not the default.

    The call proceeds past the guard; whatever it does afterwards is the
    pre-existing behaviour, so the assertion is only that it is not the 409.
    """
    monkeypatch.setattr(execution, "get_agent_service", lambda: _Agent(running=True))

    try:
        await execution.recover_task(
            TASK,
            execution.RecoverTaskRequest(force=True),
            _access={"role": "member"},
        )
    except HTTPException as exc:  # pragma: no cover - only on an unrelated failure
        assert exc.status_code != 409, "force must bypass the live-build refusal"


@pytest.mark.asyncio
async def test_recover_proceeds_when_nothing_is_running(
    project: None, monkeypatch: pytest.MonkeyPatch
):
    """The guard must not block the case recovery exists for: a dead build."""
    monkeypatch.setattr(execution, "get_agent_service", lambda: _Agent(running=False))

    try:
        await execution.recover_task(
            TASK, execution.RecoverTaskRequest(), _access={"role": "member"}
        )
    except HTTPException as exc:  # pragma: no cover - only on an unrelated failure
        assert exc.status_code != 409, "a stopped build must still be recoverable"
