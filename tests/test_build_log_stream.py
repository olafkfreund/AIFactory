"""RFC-0017 #680 — Job-native log streaming tests.

Unit-level, no real cluster: ``KubeJobLogStreamer`` is exercised against a fake
``line_source`` (an async iterator of log-line bytes) and fake sinks, asserting
that streamed lines reach BOTH sinks the in-pod path feeds — the cockpit log
sink (decoded lines) and the rmux feed (raw bytes, CRLF-normalised) — and that
the streamer is best-effort: a raising line source / sink never propagates.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# web-server server package on path (mirrors the kubejob backend tests).
_REPO = Path(__file__).resolve().parents[1]
_WS = _REPO / "apps" / "web-server"
if str(_WS) not in sys.path:
    sys.path.append(str(_WS))

from server.services.build_log_stream import KubeJobLogStreamer  # noqa: E402

pytestmark = pytest.mark.asyncio


def _source_from(lines: list[bytes]):
    """Build a ``line_source`` factory yielding ``lines`` for any (ns, job)."""

    async def _src(_namespace: str, _job_name: str) -> AsyncIterator[bytes]:
        for line in lines:
            yield line

    return _src


async def test_lines_reach_both_sinks() -> None:
    cockpit: list[str] = []
    rmux: list[tuple[str, bytes]] = []

    async def _cockpit(line: str) -> None:
        cockpit.append(line)

    def _rmux(spec_id: str, data: bytes) -> None:
        rmux.append((spec_id, data))

    streamer = KubeJobLogStreamer(
        log_sink=_cockpit,
        rmux_feed=_rmux,
        line_source=_source_from([b"building...\n", b"done\n"]),
    )
    n = await streamer.stream(namespace="factory", job_name="job-x", spec_id="007")

    assert n == 2
    # Cockpit: decoded + right-stripped lines.
    assert cockpit == ["building...", "done"]
    # rmux: raw bytes with \n -> \r\n (xterm CRLF), keyed by spec_id.
    assert rmux == [("007", b"building...\r\n"), ("007", b"done\r\n")]


async def test_empty_and_blank_lines_skipped_for_cockpit() -> None:
    cockpit: list[str] = []
    rmux: list[bytes] = []

    async def _cockpit(line: str) -> None:
        cockpit.append(line)

    def _rmux(_spec: str, data: bytes) -> None:
        rmux.append(data)

    streamer = KubeJobLogStreamer(
        log_sink=_cockpit,
        rmux_feed=_rmux,
        line_source=_source_from([b"", b"   \n", b"real\n"]),
    )
    n = await streamer.stream(namespace="f", job_name="j", spec_id="s")

    # Empty raw bytes are skipped entirely (no delivery); a blank decoded line
    # is skipped for the cockpit but the (non-empty) raw bytes still feed rmux.
    assert n == 2  # "   \n" and "real\n" are non-empty raw lines
    assert cockpit == ["real"]
    assert rmux == [b"   \r\n", b"real\r\n"]


async def test_no_rmux_feed_still_streams_cockpit() -> None:
    cockpit: list[str] = []

    async def _cockpit(line: str) -> None:
        cockpit.append(line)

    streamer = KubeJobLogStreamer(
        log_sink=_cockpit,
        rmux_feed=None,
        line_source=_source_from([b"a\n", b"b\n"]),
    )
    n = await streamer.stream(namespace="f", job_name="j", spec_id="s")

    assert n == 2
    assert cockpit == ["a", "b"]


async def test_line_source_error_is_best_effort() -> None:
    cockpit: list[str] = []

    async def _cockpit(line: str) -> None:
        cockpit.append(line)

    async def _bad_source(_ns: str, _job: str) -> AsyncIterator[bytes]:
        yield b"first\n"
        raise RuntimeError("stream dropped mid-flight")

    streamer = KubeJobLogStreamer(log_sink=_cockpit, line_source=_bad_source)
    # Must NOT raise — logs are observability, not correctness.
    n = await streamer.stream(namespace="f", job_name="j", spec_id="s")

    assert n == 1
    assert cockpit == ["first"]


async def test_cockpit_sink_error_does_not_starve_rmux() -> None:
    rmux: list[bytes] = []

    async def _bad_cockpit(_line: str) -> None:
        raise RuntimeError("cockpit broadcast failed")

    def _rmux(_spec: str, data: bytes) -> None:
        rmux.append(data)

    streamer = KubeJobLogStreamer(
        log_sink=_bad_cockpit,
        rmux_feed=_rmux,
        line_source=_source_from([b"x\n", b"y\n"]),
    )
    n = await streamer.stream(namespace="f", job_name="j", spec_id="s")

    # A raising cockpit sink is swallowed per-line; rmux still gets every line.
    assert n == 2
    assert rmux == [b"x\r\n", b"y\r\n"]


async def test_cancelled_propagates() -> None:
    async def _cockpit(_line: str) -> None:
        return None

    async def _hang_source(_ns: str, _job: str) -> AsyncIterator[bytes]:
        raise __import__("asyncio").CancelledError
        yield b""  # unreachable; makes this an async generator

    streamer = KubeJobLogStreamer(log_sink=_cockpit, line_source=_hang_source)
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        await streamer.stream(namespace="f", job_name="j", spec_id="s")
