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
