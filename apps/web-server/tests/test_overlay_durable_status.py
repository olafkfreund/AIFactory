"""W2 (Factory #218): /api/tasks overlays the durable lifecycle onto tasks.

On the packed / out-of-band execution path task_logs.json never reaches the
control plane, so spec_to_task falls back to ``backlog`` for a task that
actually ran -- making the portals and CFactory show a finished or running task
as queued. ``overlay_durable_status`` reads the authoritative durable job-state
store and corrects the stale status, without ever overriding a real one.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from server.routes.task_models import Task
from server.routes.task_service import overlay_durable_status


def _task(spec_id: str = "001-x", status: str = "backlog") -> Task:
    return Task(
        id=f"proj:{spec_id}",
        spec_id=spec_id,
        project_id="proj",
        title="t",
        description="d",
        status=status,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


def _run(tasks, states, *, enabled: bool = True):
    """Apply the overlay with a faked durable store keyed by job id."""
    store = MagicMock()
    store.get_state = AsyncMock(side_effect=lambda jid: states.get(jid))
    svc = MagicMock()
    svc._store.return_value = store
    with (
        patch(
            "server.services.job_state_store.store_enabled",
            return_value=enabled,
        ),
        patch(
            "server.services.agent_service.get_agent_service",
            return_value=svc,
        ),
    ):
        asyncio.run(overlay_durable_status(tasks))
    return store


def test_done_maps_to_human_review_completed():
    task = _task()
    _run([task], {"proj:001-x": {"lifecycle_state": "done"}})
    assert task.status == "human_review"
    assert task.review_reason == "completed"


def test_failed_maps_to_human_review_errors():
    task = _task()
    _run([task], {"proj:001-x": {"lifecycle_state": "failed"}})
    assert task.status == "human_review"
    assert task.review_reason == "errors"


def test_running_maps_to_in_progress():
    task = _task()
    _run([task], {"proj:001-x": {"lifecycle_state": "running"}})
    assert task.status == "in_progress"
    assert task.review_reason is None


def test_queued_stays_backlog():
    task = _task()
    _run([task], {"proj:001-x": {"lifecycle_state": "queued"}})
    assert task.status == "backlog"


def test_no_durable_row_leaves_task_unchanged():
    task = _task()
    _run([task], {})  # get_state returns None
    assert task.status == "backlog"


def test_real_status_is_never_overridden():
    # A task whose on-disk status already resolved is left alone even if the
    # durable row says otherwise -- the overlay only repairs the stale default.
    task = _task(status="human_review")
    store = _run([task], {"proj:001-x": {"lifecycle_state": "running"}})
    assert task.status == "human_review"
    store.get_state.assert_not_called()


def test_store_disabled_is_a_noop():
    task = _task()
    store = _run([task], {"proj:001-x": {"lifecycle_state": "done"}}, enabled=False)
    assert task.status == "backlog"
    store.get_state.assert_not_called()
