"""A reaped failed build must reach the task, not just the job-state row (#1430).

#852 found this asymmetry for ``done`` and fixed only that side. ``_fail`` still
wrote the job-state row and stopped, and nothing downstream reads that row: task
status lives in the plan. So a failed build kept reporting its last phase --
the cockpit showed a LIVE AGENT for a build that had died 50 minutes earlier,
the dispatching card never left "dispatched", and a re-run would adopt it.

The first attempt at this fix guarded ``_update_plan_status`` instead, which the
reaper never calls. It passed its own tests and changed nothing in production:
a guard written but not wired. These tests pin the WIRING -- that ``_fail``
actually invokes the hook -- because that is the half that was missing.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services.build_backend import KubeJobBuildBackend  # noqa: E402


class _Store:
    def __init__(self) -> None:
        self.terminal: list[tuple[str, str]] = []

    async def mark_terminal(self, job_id, state, error=None):  # noqa: ANN001
        self.terminal.append((job_id, state))


@pytest.mark.asyncio
async def test_fail_invokes_the_hook() -> None:
    """The wiring. Without it the row is written and the task never hears."""
    seen: list[tuple[str, str]] = []

    async def on_fail(job_id: str, reason: str) -> None:
        seen.append((job_id, reason))

    be = KubeJobBuildBackend(_Store(), on_fail=on_fail)
    await be._fail("p:158-spec", "k8s Job reported failed")

    assert seen == [("p:158-spec", "k8s Job reported failed")]


@pytest.mark.asyncio
async def test_the_row_is_still_written() -> None:
    """The hook is additive: the job-state row must survive the change."""
    store = _Store()
    be = KubeJobBuildBackend(store, on_fail=None)
    await be._fail("p:158-spec", "boom")

    assert store.terminal == [("p:158-spec", "failed")]


@pytest.mark.asyncio
async def test_a_raising_hook_does_not_crash_the_reaper() -> None:
    """The reconcile loop must never die on bookkeeping, or one bad build
    strands every later one."""

    async def on_fail(job_id: str, reason: str) -> None:
        raise RuntimeError("hook exploded")

    be = KubeJobBuildBackend(_Store(), on_fail=on_fail)
    await be._fail("p:158-spec", "boom")  # must not raise


@pytest.mark.asyncio
async def test_no_hook_when_the_row_write_fails() -> None:
    """If the build could not be marked failed, do not tell downstream it was.
    Reporting a terminal state that was not recorded is worse than silence."""
    seen: list[str] = []

    class _Broken:
        async def mark_terminal(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("store down")

    async def on_fail(job_id: str, reason: str) -> None:
        seen.append(job_id)

    be = KubeJobBuildBackend(_Broken(), on_fail=on_fail)
    await be._fail("p:158-spec", "boom")

    assert seen == []


@pytest.mark.asyncio
async def test_absent_hook_is_still_supported() -> None:
    """Tests and any caller that does not need the hook keep working."""
    be = KubeJobBuildBackend(_Store())
    await be._fail("p:158-spec", "boom")  # must not raise


# ── the SUCCESS side had the same hole (#1430) ────────────────────────────────
#
# Spec 160 finished with all four subtasks completed, pushed a branch with the
# contracted API, handed off to TFactory with a 200 -- and still read
# "in_progress / planning" to the task API and "dispatched" to the card that
# requested it. The failure case was the visible one (a LIVE AGENT for a dead
# build); this one is worse, because the sequence cannot advance and the card
# never reaches its next stage even though the work is done.


class _Recorder:
    """The two calls _on_kubejob_build_done makes, without the real mixin host."""

    def __init__(self, effective: str = "completed") -> None:
        self.effective = effective
        self.recorded: list[tuple[str, str]] = []
        self.released: list[str] = []
        self.drained = 0

    def _release_task_credential(self, job_id: str) -> None:
        self.released.append(job_id)

    async def _emit_kubejob_terminal_completion(self, job_id: str) -> str:
        return self.effective

    async def _record_kubejob_terminal(self, job_id: str, status: str) -> None:
        self.recorded.append((job_id, status))

    async def _drain_queue(self) -> None:
        self.drained += 1


@pytest.mark.asyncio
async def test_a_successful_build_records_completed() -> None:
    from server.services.agent_kubejob import KubejobMixin

    r = _Recorder("completed")
    await KubejobMixin._on_kubejob_build_done(r, "p:160-spec")

    assert r.recorded == [("p:160-spec", "completed")]


@pytest.mark.asyncio
async def test_the_evidence_gate_downgrade_is_what_gets_recorded() -> None:
    """run_terminal_completion downgrades "completed" to "failed" when the build
    wrote nothing. Recording the optimistic value would put "completed" in the
    plan for a build that function already ruled failed."""
    from server.services.agent_kubejob import KubejobMixin

    r = _Recorder("failed")
    await KubejobMixin._on_kubejob_build_done(r, "p:160-spec")

    assert r.recorded == [("p:160-spec", "failed")]


@pytest.mark.asyncio
async def test_bookkeeping_still_happens_on_success() -> None:
    """Credential release and queue drain must survive the change, or a finished
    build strands a credential and blocks the FIFO behind it."""
    from server.services.agent_kubejob import KubejobMixin

    r = _Recorder()
    await KubejobMixin._on_kubejob_build_done(r, "p:160-spec")

    assert r.released == ["p:160-spec"]
    assert r.drained == 1


# ── the return-value plumbing itself (mutation C) ─────────────────────────────
#
# The tests above fake _emit_kubejob_terminal_completion, so replacing
# run_terminal_completion's `return terminal_status` with a hardcoded
# "completed" changed nothing and the mutation SURVIVED. The downgrade is the
# whole reason the caller asks rather than assumes, so it needs its own test.


@pytest.mark.asyncio
async def test_run_terminal_completion_returns_the_downgraded_status(
    tmp_path: Path, monkeypatch
) -> None:
    """An empty build is downgraded to "failed" inside the completion path. The
    caller records what came back, so if this returns the optimistic value the
    plan says "completed" for a build already ruled failed."""
    from server.services import completion_orchestration as co

    monkeypatch.setattr(co, "_build_wrote_nothing", lambda *a, **k: True)

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()

    result = await co.run_terminal_completion(
        spec_dir=spec_dir,
        project_path=tmp_path,
        spec_id="160-spec",
        task_id="p:160-spec",
        backend_path=None,
        is_terminal=False,  # skip the emit; the downgrade is what matters
        is_completed=True,
        terminal_status="completed",
        logger=logging.getLogger("test"),
    )

    assert result == "failed"


@pytest.mark.asyncio
async def test_it_returns_completed_when_the_build_wrote_something(
    tmp_path: Path, monkeypatch
) -> None:
    from server.services import completion_orchestration as co

    monkeypatch.setattr(co, "_build_wrote_nothing", lambda *a, **k: False)

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()

    result = await co.run_terminal_completion(
        spec_dir=spec_dir,
        project_path=tmp_path,
        spec_id="160-spec",
        task_id="p:160-spec",
        backend_path=None,
        is_terminal=False,
        is_completed=True,
        terminal_status="completed",
        logger=logging.getLogger("test"),
    )

    assert result == "completed"
