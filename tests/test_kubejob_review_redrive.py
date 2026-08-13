"""#1249 — the #260 review re-drive must reach a kubejob build.

``check_review_obligation`` (the #260 nudge/escalate authority) used to have
exactly one caller: ``process_monitor.monitor_process``, which takes a live
``asyncio.subprocess.Process``. The kubejob backend has no local subprocess,
so a peer review requested but never started on a kubejob build was never
nudged and never escalated — silently, because the failure mode is the
absence of an error.

This drives ``AgentService.reconcile_kubejob_builds`` — the SAME tick every
active kubejob row already passes through for terminal-state polling — end to
end against the real ``qa.review_redrive`` orchestrator and asserts the
escalation actually lands in the MAIN spec dir's ``task_control.json``, even
though the cycle file only ever existed in the WORKTREE spec dir (exactly the
live-cluster shape the issue measured: cycle files in worktree copies, none
in main).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))
_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from qa.review_cycle import request_review  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402

TIMEOUT = 300.0


def _aged(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class _FakeBackend:
    """reconcile_by_poll always reports the build still running (terminal=None)."""

    async def reconcile_by_poll(self, _job_id: str) -> str | None:
        return None


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A project with a build's worktree spec dir carrying a stuck review cycle.

    Main spec dir starts EMPTY — matching the live-cluster evidence in #1249.
    """
    project_path = tmp_path / "proj"
    worktree_spec = (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / "001-feature"
        / ".aifactory"
        / "specs"
        / "001-feature"
    )
    main_spec = project_path / ".aifactory" / "specs" / "001-feature"
    worktree_spec.mkdir(parents=True)
    # main_spec deliberately NOT created — mirrors the live cluster.

    request_review(worktree_spec)
    # Age the request past the timeout by rewriting requested_at directly
    # (request_review always stamps "now").
    cycle_path = worktree_spec / "qa_review_cycle.json"
    data = json.loads(cycle_path.read_text())
    data["requested_at"] = _aged(-(TIMEOUT + 1))
    cycle_path.write_text(json.dumps(data))

    monkeypatch.setattr(
        "server.routes.projects.resolve_project_path",
        lambda pid: project_path if pid == "p1" else (_ for _ in ()).throw(KeyError()),
    )
    return project_path, main_spec, worktree_spec


async def _make_service(monkeypatch: pytest.MonkeyPatch) -> AgentService:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    service = AgentService()
    service._store_enabled = True

    class _FakeStore:
        async def get_active_kubejobs(self) -> list[dict[str, Any]]:
            return [{"job_id": "p1:001-feature"}]

    monkeypatch.setattr(service, "_store", lambda: _FakeStore())
    monkeypatch.setattr(service, "_build_backend", lambda: _FakeBackend())
    return service


async def test_review_redrive_reaches_kubejob_build(
    project: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path, main_spec, worktree_spec = project
    service = await _make_service(monkeypatch)

    # THE regression: before #1249's fix, reconcile_kubejob_builds never calls
    # check_review_obligation at all, so nothing nudges the stuck review.
    out = await service.reconcile_kubejob_builds()

    assert out == {}  # still running, nothing terminal this tick
    # First strike: an inbox nudge was delivered to the WORKTREE inbox.
    msgs_path = worktree_spec / "inbox"
    assert msgs_path.exists(), "expected an inbox nudge in the worktree spec dir"

    # Second tick, one window later, still untouched → escalate to human_review.
    cycle_path = main_spec / "qa_review_cycle.json"
    data = json.loads(cycle_path.read_text())
    data["last_redrive_at"] = _aged(-(TIMEOUT + 1))
    cycle_path.write_text(json.dumps(data))

    await service.reconcile_kubejob_builds()

    control = json.loads((main_spec / "task_control.json").read_text())
    assert control["status"] == "human_review"
    assert "stalled" in control["reviewReason"].lower()


async def test_review_redrive_noop_when_backend_not_kubejob(
    project: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: with the kubejob backend OFF, reconcile is a no-op (unrelated path)."""
    project_path, main_spec, worktree_spec = project
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    service = AgentService()
    service._store_enabled = True

    out = await service.reconcile_kubejob_builds()
    assert out == {}
    assert not (main_spec / "task_control.json").exists()
