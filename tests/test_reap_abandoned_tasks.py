"""#1001: reap a task stuck ``in_progress`` with no live build behind it.

A build's Job can die without a terminal event (killed pod, node drain,
control-plane roll, cleared job-state row); the task then lists as in_progress
forever and the cockpit shows it as running. reap_abandoned_tasks marks it
failed once it is provably not backed by any live build and has gone stale.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services.agent_kubejob import KubejobMixin  # noqa: E402


class _Reaper(KubejobMixin):
    """Minimal stand-in exposing the reaper's collaborators as overridable seams."""

    def __init__(
        self, *, running: set[str] | None = None, live: set[str] | None = None
    ):
        self._running = running or set()
        self._live = live or set()
        self.reaped_calls: list[tuple[str, str]] = []

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    async def _has_live_kubejob(self, task_id: str) -> bool:
        return task_id in self._live

    async def _update_plan_status(self, project_path, spec_id, status, task_id, **kw):
        self.reaped_calls.append((task_id, status))


def _iso(delta_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta_seconds)).isoformat()


def _task(name: str, status: str, updated_at: str):
    return SimpleNamespace(id=f"p:{name}", status=status, updated_at=updated_at)


def _patch_enum(monkeypatch, tasks: list[tuple[str, object]]):
    """Wire load_projects/get_spec_dirs/spec_to_task to a single project 'p'."""
    monkeypatch.setattr(
        "server.project_registry.load_projects", lambda: {"p": {"path": "/x"}}
    )
    dirs = {name: Path("/x/specs") / name for name, _ in tasks}
    monkeypatch.setattr(
        "server.routes.task_service.get_spec_dirs", lambda pp: list(dirs.values())
    )
    by_dir = {dirs[name]: t for name, t in tasks}
    monkeypatch.setattr(
        "server.routes.task_service.spec_to_task", lambda pid, sd: by_dir[sd]
    )


# ── _task_stale ─────────────────────────────────────────────────────────────


def test_task_stale():
    now = datetime.now(UTC)
    assert KubejobMixin._task_stale(_iso(-3600), now, 600) is True  # 1h old > 600s
    assert KubejobMixin._task_stale(_iso(-10), now, 600) is False  # 10s old
    assert KubejobMixin._task_stale("not-a-date", now, 600) is False  # unknown → safe


# ── reap_abandoned_tasks ────────────────────────────────────────────────────


async def test_reaps_stale_in_progress_with_no_live_build(monkeypatch):
    _patch_enum(
        monkeypatch, [("078-gcd", _task("078-gcd", "in_progress", _iso(-3600)))]
    )
    r = _Reaper()
    assert await r.reap_abandoned_tasks(deadline_seconds=600) == ["p:078-gcd"]
    assert r.reaped_calls == [("p:078-gcd", "failed")]


async def test_skips_fresh_in_progress(monkeypatch):
    _patch_enum(monkeypatch, [("x", _task("x", "in_progress", _iso(-10)))])
    r = _Reaper()
    assert await r.reap_abandoned_tasks(deadline_seconds=600) == []
    assert r.reaped_calls == []


async def test_skips_live_subprocess_build(monkeypatch):
    _patch_enum(monkeypatch, [("x", _task("x", "in_progress", _iso(-3600)))])
    r = _Reaper(running={"p:x"})  # a live subprocess build in THIS pod
    assert await r.reap_abandoned_tasks(deadline_seconds=600) == []


async def test_skips_live_kubejob(monkeypatch):
    _patch_enum(monkeypatch, [("x", _task("x", "in_progress", _iso(-3600)))])
    r = _Reaper(live={"p:x"})  # a running durable job-state row backs it
    assert await r.reap_abandoned_tasks(deadline_seconds=600) == []


async def test_skips_non_running_statuses(monkeypatch):
    _patch_enum(
        monkeypatch,
        [
            ("a", _task("a", "human_review", _iso(-3600))),
            ("b", _task("b", "done", _iso(-3600))),
            ("c", _task("c", "backlog", _iso(-3600))),
        ],
    )
    r = _Reaper()
    assert await r.reap_abandoned_tasks(deadline_seconds=600) == []
