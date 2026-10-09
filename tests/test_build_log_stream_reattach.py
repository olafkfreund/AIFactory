"""#1619 — the streamer must outlive an early EOF and wait for the container.

Two defects, one symptom. On task 022 the control plane logged

    09:22:35  pod created
    09:22:37  "log stream completed (1 line(s))"
    09:24:03  the app container actually started

and then followed nothing for the remaining 145 minutes of the build. So
``task_logs.json`` stayed at 0 entries, the phase never left ``planning``, the
progress bar never moved and the live console stayed blank — while #1110's
parser, which turns those log lines into all of the above, sat ready and unfed.

The pod existed almost immediately; its app container did not, because a build
pod runs three init containers first. Following a not-yet-started container
yields an immediately-ending stream, and the old pump treated that clean EOF as
completion.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.services.build_log_stream import (  # noqa: E402
    KubeJobLogStreamer,
    _pod_has_started,
)


class _Pod:
    def __init__(self, phase: str) -> None:
        self.status = type("S", (), {"phase": phase})()


def test_pending_pod_is_not_ready_to_follow():
    """A pod in Pending is still running its init containers.

    THE regression: the old wait returned as soon as the pod object existed.
    """
    assert _pod_has_started(_Pod("Pending")) is False


@pytest.mark.parametrize("phase", ["Running", "Succeeded", "Failed"])
def test_started_pod_is_ready_to_follow(phase: str):
    """Once the app containers have been started there is a log to follow."""
    assert _pod_has_started(_Pod(phase)) is True


def test_pod_without_status_is_not_ready():
    """A malformed/incomplete pod must not be treated as ready."""
    assert _pod_has_started(object()) is False


def _source(batches: list[list[bytes]]):
    """A line source that yields a different batch on each attach."""
    calls = {"n": 0}

    async def factory(_ns: str, _job: str) -> AsyncIterator[bytes]:
        i = min(calls["n"], len(batches) - 1)
        calls["n"] += 1
        for line in batches[i]:
            yield line

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


@pytest.mark.asyncio
async def test_reattaches_while_the_job_is_active(monkeypatch: pytest.MonkeyPatch):
    """An EOF while the Job runs means reattach, not done.

    The first attach yields nothing — the container-not-started case — and the
    second carries the build's real output.
    """
    monkeypatch.setattr(
        "server.services.build_log_stream._REATTACH_INTERVAL_SECONDS", 0.0
    )
    seen: list[str] = []
    active = {"v": True}

    async def _job_active() -> bool:
        return active["v"]

    src = _source([[], [b"phase: coding\n", b"done\n"]])

    async def _sink(line: str) -> None:
        seen.append(line)
        if len(seen) == 2:
            active["v"] = False  # the Job finishes after its last line

    streamer = KubeJobLogStreamer(log_sink=_sink, line_source=src)
    delivered = await streamer.stream(
        namespace="f", job_name="j", spec_id="s", job_active=_job_active
    )

    assert seen == ["phase: coding", "done"]
    assert delivered == 2
    assert src.calls["n"] >= 2, "must have reattached after the empty first pass"


@pytest.mark.asyncio
async def test_stops_when_the_job_is_no_longer_active(monkeypatch: pytest.MonkeyPatch):
    """A finished Job's EOF really is the end — no spinning."""
    monkeypatch.setattr(
        "server.services.build_log_stream._REATTACH_INTERVAL_SECONDS", 0.0
    )

    async def _job_active() -> bool:
        return False

    src = _source([[b"only line\n"]])
    streamer = KubeJobLogStreamer(log_sink=_noop_sink, line_source=src)
    delivered = await streamer.stream(
        namespace="f", job_name="j", spec_id="s", job_active=_job_active
    )

    assert delivered == 1
    assert src.calls["n"] == 1, "must not reattach once the Job is done"


@pytest.mark.asyncio
async def test_replayed_lines_are_not_delivered_twice(
    monkeypatch: pytest.MonkeyPatch,
):
    """Re-following returns the log from the start; the seam must not duplicate.

    Without the already-consumed offset the cockpit would receive the whole log
    again on every reattach.
    """
    monkeypatch.setattr(
        "server.services.build_log_stream._REATTACH_INTERVAL_SECONDS", 0.0
    )
    seen: list[str] = []
    attach = {"n": 0}

    async def _job_active() -> bool:
        return attach["n"] < 2

    async def src(_ns: str, _job: str) -> AsyncIterator[bytes]:
        attach["n"] += 1
        # The second attach replays line 1 and adds line 2.
        for line in [b"one\n"] if attach["n"] == 1 else [b"one\n", b"two\n"]:
            yield line

    async def _sink(line: str) -> None:
        seen.append(line)

    streamer = KubeJobLogStreamer(log_sink=_sink, line_source=src)
    await streamer.stream(
        namespace="f", job_name="j", spec_id="s", job_active=_job_active
    )

    assert seen == ["one", "two"], f"line replayed to the cockpit: {seen}"


@pytest.mark.asyncio
async def test_gives_up_loudly_after_persistent_silence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """An active Job that never produces output must not spin forever.

    And it must say so: the old code logged "completed" at INFO after one line
    against a 145-minute build, which read like success.
    """
    monkeypatch.setattr(
        "server.services.build_log_stream._REATTACH_INTERVAL_SECONDS", 0.0
    )
    monkeypatch.setattr("server.services.build_log_stream._MAX_EMPTY_REATTACHES", 3)

    async def _job_active() -> bool:
        return True

    src = _source([[]])
    streamer = KubeJobLogStreamer(log_sink=_noop_sink, line_source=src)
    with caplog.at_level("WARNING"):
        delivered = await streamer.stream(
            namespace="f", job_name="j", spec_id="s", job_active=_job_active
        )

    assert delivered == 0
    assert src.calls["n"] <= 5, "the reattach loop must be bounded"
    assert any(
        "reattach" in r.message or "still active" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_single_pass_without_a_liveness_check() -> None:
    """No ``job_active`` injected → one pass, the pre-#1619 behaviour."""
    src = _source([[b"a\n"], [b"b\n"]])
    streamer = KubeJobLogStreamer(log_sink=_noop_sink, line_source=src)
    delivered = await streamer.stream(namespace="f", job_name="j", spec_id="s")

    assert delivered == 1
    assert src.calls["n"] == 1


async def _noop_sink(_line: str) -> None:
    return None
