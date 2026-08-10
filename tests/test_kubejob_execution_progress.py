#!/usr/bin/env python3
"""A running k8s-Job build must report live progress, not null (#1110).

On the live cluster `AIFACTORY_BUILD_BACKEND=kubejob`, so a build is dispatched
as a Job rather than spawned in-pod. `_spawn_task_execution` is the ONLY place
that constructs a `TaskLogWriter`, and the kubejob backend does not go through
it (`_start_build_unit` branches before that). So `task_logs.json` never
appeared beside the spec, and `get_execution_progress` — which reads exactly
that file and returns None when it is absent — returned None for the entire
build. The Job writes its own copy inside its /work emptyDir and pushes it back
only at the END, which is why every phase arrived in one step at completion.

The Job's stdout already reaches the control plane: `KubeJobLogStreamer` follows
the pod's logs into the cockpit pane. It carries the same `[PHASE_EVENT]` lines
the in-pod subprocess emits, and nothing read them. The fix routes those lines
through the SAME `_handle_output_line` the in-pod path uses.

Found while capturing the cockpit hero-shot demo artifacts for Factory#244.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.routes.task_service import get_execution_progress  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402
from server.services.task_phase import TaskPhase  # noqa: E402

TASK_ID = "p1:099-link-shortener-service-python-"
SPEC_ID = "099-link-shortener-service-python-"

# Verbatim `core.phase_event.emit_phase` output — produced by running the real
# emitter, so the fixture cannot drift from what run.py actually prints inside
# the Job pod and KubeJobLogStreamer actually delivers.
JOB_STDOUT = [
    '__EXEC_PHASE__:{"phase": "planning", "message": "Loading trusted plan"}',
    "Read(implementation_plan.json)",
    '__EXEC_PHASE__:{"phase": "coding", "message": "C1: GET /healthz returns 200",'
    ' "progress": 10, "subtask": "C1"}',
    "Write(app/routers/health.py)",
    '__EXEC_PHASE__:{"phase": "coding", "message": "C2: POST /links",'
    ' "progress": 20, "subtask": "C2"}',
]


@pytest.fixture
def project(tmp_path):
    spec_dir = tmp_path / ".aifactory" / "specs" / SPEC_ID
    spec_dir.mkdir(parents=True)
    return tmp_path, spec_dir


def _service() -> AgentService:
    service = AgentService()
    service._emit_progress = AsyncMock()
    service._emit_log = AsyncMock()
    return service


async def _dispatch_and_stream(service: AgentService, project_path: Path, lines):
    """Drive the real kubejob log-stream wiring with a fake Job log."""
    captured: dict = {}

    class _FakeStreamer:
        # ``**_rest`` so this double does not have to be edited every time the
        # real streamer gains a constructor argument these tests do not care
        # about (``plan_sync``, #1228). The double exists to capture the log
        # sink; pinning the full signature here only ever fails for the wrong
        # reason.
        def __init__(self, *, log_sink, rmux_feed=None, **_rest):
            captured["sink"] = log_sink

        async def stream(self, **_kwargs):
            for line in lines:
                await captured["sink"](line)

    with (
        patch.object(
            service,
            "_kubejob_worker_ref",
            AsyncMock(return_value=("factory", "aifactory-build-099")),
        ),
        patch(
            "server.services.build_log_stream.KubeJobLogStreamer",
            _FakeStreamer,
        ),
    ):
        await service._start_kubejob_log_stream(
            task_id=TASK_ID, project_path=project_path, spec_id=SPEC_ID
        )
        streamer_task = service._kubejob_log_streamers.get(TASK_ID)
        if streamer_task is not None:
            await streamer_task


async def test_a_dispatched_job_reports_a_phase_immediately(project):
    """Before the first log line arrives, the task must not read as `null`."""
    project_path, spec_dir = project
    service = _service()

    with patch.object(service, "_kubejob_worker_ref", AsyncMock(return_value=None)):
        await service._start_kubejob_log_stream(
            task_id=TASK_ID, project_path=project_path, spec_id=SPEC_ID
        )

    progress = get_execution_progress(spec_dir, [])
    assert progress is not None, (
        "a dispatched build reports executionProgress: null, so the cockpit "
        "cannot tell a working build from a wedged one"
    )
    assert progress["phase"] == "planning"


async def test_job_phase_events_move_execution_progress(project):
    """The real defect: the Job's [PHASE_EVENT] lines were never read."""
    project_path, spec_dir = project
    service = _service()

    await _dispatch_and_stream(service, project_path, JOB_STDOUT)

    progress = get_execution_progress(spec_dir, [])
    assert progress is not None
    assert progress["phase"] == "coding", (
        f"the build moved to coding and the control plane still reports "
        f"{progress['phase']!r}"
    )
    assert progress["startedAt"]


async def test_the_phase_transition_is_recorded_not_just_the_latest(project):
    """planning must be marked completed, not left dangling as active."""
    project_path, spec_dir = project
    service = _service()

    await _dispatch_and_stream(service, project_path, JOB_STDOUT)

    phases = json.loads((spec_dir / "task_logs.json").read_text())["phases"]
    assert phases["planning"]["status"] == "completed"
    assert phases["coding"]["status"] == "active"


async def test_progress_is_emitted_to_the_cockpit_per_phase_event(project):
    """The WebSocket feed has to move too, not only the file."""
    project_path, _spec_dir = project
    service = _service()

    await _dispatch_and_stream(service, project_path, JOB_STDOUT)

    emitted = [call.args[0] for call in service._emit_progress.call_args_list]
    assert len(emitted) == 3, f"expected one per PHASE_EVENT, got {len(emitted)}"
    assert [p.phase for p in emitted] == [
        TaskPhase.PLANNING,
        TaskPhase.CODING,
        TaskPhase.CODING,
    ]
    assert emitted[-1].subtask == "C2"
    assert emitted[-1].percentage == 20


async def test_the_log_pane_still_receives_every_line(project):
    """Mutation guard: reading the lines must not stop forwarding them.

    `_handle_output_line` emits the TaskLog the old sink emitted, so the cockpit
    log pane is unchanged. If a future refactor drops that, the pane goes dark.
    """
    project_path, _spec_dir = project
    service = _service()

    await _dispatch_and_stream(service, project_path, JOB_STDOUT)

    forwarded = [call.args[0].content for call in service._emit_log.call_args_list]
    assert forwarded == JOB_STDOUT


async def test_a_build_that_never_streams_still_has_a_readable_phase(project):
    """No worker_ref (streaming unavailable) must not restore the null."""
    project_path, spec_dir = project
    service = _service()

    with patch.object(service, "_kubejob_worker_ref", AsyncMock(return_value=None)):
        await service._start_kubejob_log_stream(
            task_id=TASK_ID, project_path=project_path, spec_id=SPEC_ID
        )

    assert (spec_dir / "task_logs.json").exists()
    assert get_execution_progress(spec_dir, [])["phase"] == "planning"


async def test_reporting_failure_never_breaks_a_dispatch(project, caplog):
    """A build must run even when its progress file cannot be opened."""
    project_path, _spec_dir = project
    service = _service()

    with (
        patch.object(service, "_kubejob_worker_ref", AsyncMock(return_value=None)),
        patch(
            "server.services.agent_kubejob.TaskLogWriter",
            MagicMock(side_effect=OSError("read-only filesystem")),
        ),
        caplog.at_level("WARNING"),
    ):
        await service._start_kubejob_log_stream(
            task_id=TASK_ID, project_path=project_path, spec_id=SPEC_ID
        )

    assert "report no live progress" in caplog.text


def test_the_in_pod_path_and_the_job_path_share_one_handler():
    """One engine, no drift: two copies of phase parsing is how one goes stale."""
    import inspect

    from server.services import agent_emit, agent_kubejob

    assert "self._handle_output_line(" in inspect.getsource(
        agent_emit.EmitMixin._process_output
    )
    assert "self._handle_output_line(" in inspect.getsource(
        agent_kubejob.KubejobMixin._start_kubejob_log_stream
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
