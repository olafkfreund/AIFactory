"""#1228 — per-subtask status must reach the control plane DURING a build.

The defect: per-subtask ``status``/``started_at`` live in
``implementation_plan.json``, which on the packed path is written inside the
Job's ephemeral ``/work``. The push (Job side) and the pull (control-plane side)
both already existed but were each called exactly once, at the end — so
CFactory's live execution DAG rendered every node ``waiting`` for a whole build
and then flipped all of them to done in one step.

These tests pin both halves:

* every plan write inside the Job publishes (``publish_plan`` at the two
  funnels), and
* the control plane pulls on the log stream's clock, throttled.
"""

import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_WEB = _ROOT / "apps" / "web-server"
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))

from server.services.build_log_stream import (  # noqa: E402
    KubeJobLogStreamer,
)


def _lines(*payloads: bytes):
    async def _source(namespace: str, job_name: str):
        for p in payloads:
            yield p

    return _source


async def _sink(line: str) -> None:
    return None


def _stream(**kw) -> int:
    streamer = KubeJobLogStreamer(log_sink=_sink, **kw)
    return asyncio.run(
        streamer.stream(namespace="ns", job_name="job", spec_id="spec-1")
    )


def test_plan_is_pulled_on_the_very_first_line() -> None:
    """A build whose subtasks all start in its opening seconds must not render
    as fully pending for a whole throttle interval."""
    calls: list[int] = []
    delivered = _stream(
        line_source=_lines(b"one\n"),
        plan_sync=lambda: calls.append(1),
    )
    assert delivered == 1
    assert len(calls) == 1, "first line must sync immediately, not after a wait"


def test_first_sync_is_not_gated_by_the_boot_clock() -> None:
    """The throttle's seed must be ``None``, not ``0.0``.

    ``loop.time()`` is ``time.monotonic()`` — time since BOOT, not since epoch.
    A ``0.0`` seed therefore reads as "synced at boot", so a build Job whose pod
    starts within one interval of a node coming up would skip its first pull and
    render a pending DAG for that whole interval. The two seeds are otherwise
    indistinguishable, which is exactly why this needs asserting directly.
    """
    assert KubeJobLogStreamer(log_sink=_sink)._plan_synced_at is None


def test_pull_is_throttled_across_a_burst() -> None:
    """The hook is per-LINE; a busy build emits thousands a minute. Without the
    throttle this is one object-store GET per log line."""
    calls: list[int] = []
    _stream(
        line_source=_lines(*[b"line\n"] * 200),
        plan_sync=lambda: calls.append(1),
        plan_sync_interval=3600.0,
    )
    assert len(calls) == 1, f"200 lines produced {len(calls)} pulls, expected 1"


def test_pull_resumes_once_the_interval_elapses() -> None:
    """Throttled is not 'once per build' — the DAG has to keep advancing."""
    calls: list[int] = []
    _stream(
        line_source=_lines(*[b"line\n"] * 5),
        plan_sync=lambda: calls.append(1),
        plan_sync_interval=0.0,
    )
    assert len(calls) == 5


def test_a_failing_pull_never_breaks_the_log_stream() -> None:
    """Plan sync is best-effort: an unreachable object store costs a live DAG,
    never the build's logs."""

    def _boom() -> None:
        raise RuntimeError("object store unreachable")

    delivered = _stream(
        line_source=_lines(b"a\n", b"b\n", b"c\n"),
        plan_sync=_boom,
        plan_sync_interval=0.0,
    )
    assert delivered == 3, "a raising plan sync must not truncate the log stream"


def test_no_plan_sync_configured_is_inert() -> None:
    """Off the packed path there is nothing to pull, and the streamer must not
    pay for a call that can never do anything."""
    assert _stream(line_source=_lines(b"a\n", b"b\n")) == 2


# --- Job side: every plan write publishes -----------------------------------


def _read(rel: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / rel).read_text()


def _code_only(text: str) -> str:
    """Strip comments and docstring prose.

    Without this these tests match the very comments that explain the fix — the
    same self-flagging trap hit twice on #1082.
    """
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith(("#", '"', "*"))
    )


def test_sync_plan_to_source_publishes_before_its_early_returns() -> None:
    """``sync_plan_to_source`` is the chokepoint for every wave-path plan write,
    but it early-returns for non-worktree builds — which advance the plan just
    the same. The publish must sit above those returns."""
    src = _code_only(_read("apps/backend/agents/utils.py"))
    body = src.split("def sync_plan_to_source", 1)[1]
    publish_at = body.index("publish_plan(spec_dir)")
    first_return = body.index("return False")
    assert publish_at < first_return, (
        "publish_plan must run BEFORE the not-in-worktree early returns, or "
        "non-worktree builds never reach the control plane mid-run"
    )


def test_publish_uses_the_freshest_copy_not_the_source() -> None:
    """``spec_dir`` is the directory the caller just wrote; the source is the
    pre-copy one. Publishing the source would ship a stale plan in worktree
    mode — the exact bug, one directory over."""
    src = _code_only(_read("apps/backend/agents/utils.py"))
    body = src.split("def sync_plan_to_source", 1)[1].split("def ", 1)[0]
    assert "publish_plan(spec_dir)" in body
    assert "publish_plan(source_spec_dir" not in body


def test_serial_funnel_publishes_too() -> None:
    """The serial path writes the plan directly rather than through
    ``sync_plan_to_source``, so it needs its own publish — otherwise a serial
    build's DAG stays frozen while a parallel one's is live."""
    src = _code_only(_read("apps/backend/agents/tools_pkg/tools/subtask.py"))
    assert "publish_plan(spec_dir)" in src, (
        "apply_subtask_status_update writes implementation_plan.json itself; "
        "without a publish the serial path's transitions never leave the Job"
    )


def test_publish_does_not_block_a_running_event_loop() -> None:
    """The push is object-store I/O (a PUT plus a boto3 client construction) and
    both funnels run inside a loop — ``apply_subtask_status_update`` is a
    coroutine, and ``sync_plan_to_source`` is called from the wave
    orchestrator's async ``run_subtask``. Blocking there stalls every concurrent
    subtask agent in the wave."""
    import time

    from agents import utils

    started = threading.Event()
    release = threading.Event()

    def _slow(_spec_dir):
        started.set()
        release.wait(5)

    async def _drive() -> float:
        t0 = time.monotonic()
        utils.publish_plan(Path("/nowhere/spec-1"))
        return time.monotonic() - t0

    with mock.patch.object(utils, "_publish_plan_now", _slow):
        elapsed = asyncio.run(_drive())
        assert started.wait(5), "the push never ran"
        release.set()

    assert elapsed < 0.5, (
        f"publish_plan blocked the event loop for {elapsed:.2f}s while the "
        "upload ran; concurrent wave subtasks are stalled for that whole time"
    )


def test_publish_runs_inline_when_there_is_no_loop_to_protect() -> None:
    """Off the loop — ``cli/main.py``'s terminal push — it must still happen,
    and happen synchronously, or the final plan can be lost at exit."""
    from agents import utils

    seen: list[Path] = []
    with mock.patch.object(utils, "_publish_plan_now", seen.append):
        utils.publish_plan(Path("/nowhere/spec-1"))
    assert seen == [Path("/nowhere/spec-1")]


def test_publishes_are_serialised_so_a_slow_one_cannot_overwrite_a_newer() -> None:
    """One worker, by design: out-of-order uploads would land an OLDER plan on
    top of a newer one and leave the DAG reading backwards until the next
    transition."""
    from agents import utils

    order: list[int] = []

    def _record(spec_dir: Path) -> None:
        n = int(spec_dir.name)
        if n == 0:
            time.sleep(0.15)  # the slow one goes FIRST
        order.append(n)

    async def _drive() -> None:
        with mock.patch.object(utils, "_publish_plan_now", _record):
            for i in range(4):
                utils.publish_plan(Path(str(i)))
            await asyncio.get_running_loop().run_in_executor(
                utils._PUBLISH_POOL, lambda: None
            )

    asyncio.run(_drive())
    assert order == [0, 1, 2, 3], f"pushes landed out of order: {order}"


def test_serial_funnel_reuses_the_shared_publish() -> None:
    """One engine: re-spelling the push here would let the two paths drift."""
    src = _read("apps/backend/agents/tools_pkg/tools/subtask.py")
    assert "from agents.utils import publish_plan" in src
    assert "maybe_push_plan" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
