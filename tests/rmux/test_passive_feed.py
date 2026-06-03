"""Passive Live-Console feed (Epic #44 follow-up).

When the agent already runs under agent_service's own PTY, rmux must NOT
re-spawn it (that double-runs the agent). Instead we register a FIFO-only
("passive") session and tee the agent's output bytes into it; the WS bridge
streams that FIFO read-only exactly as for a real rmux pane.

These tests exercise the real code path agent_service uses
(``create_passive_for_task`` + ``feed``) against a tmp_path FIFO, with a
blocking reader standing in for the bridge — no rmux binary required.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from server.rmux import session as rmux_session


@pytest.fixture()
def registry(tmp_path):
    reg = rmux_session.configure(panes_dir=tmp_path / "panes")
    yield reg
    rmux_session.reset_for_tests()


@pytest.mark.asyncio
async def test_passive_create_makes_fifo_without_spawning(registry):
    fifo = await registry.create_passive_for_task("spec-1")
    assert fifo.exists()
    state = registry.get_state("spec-1")
    assert state is not None
    assert state.passive is True
    # No rmux session is spawned for passive sessions.
    assert state.write_fd is None


@pytest.mark.asyncio
async def test_feed_streams_bytes_to_a_connected_reader(registry):
    fifo = await registry.create_passive_for_task("spec-2")

    received: list[bytes] = []

    def _reader():
        with open(fifo, "rb", buffering=0) as fh:
            while True:
                data = fh.read(4096)
                if not data:
                    return
                received.append(data)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    await asyncio.sleep(0.2)  # let the reader open the read end

    registry.feed("spec-2", b"[planner] working\r\n")
    registry.feed("spec-2", b"[coder] edit main.py +12\r\n")
    await asyncio.sleep(0.2)

    await registry.reap_for_task("spec-2")  # closes writer -> reader EOF
    t.join(timeout=2)

    out = b"".join(received)
    assert b"[planner] working" in out
    assert b"[coder] edit main.py +12" in out
    assert b"\r\n" in out  # CRLF preserved for xterm
    assert not fifo.exists()  # reaped


@pytest.mark.asyncio
async def test_feed_with_no_reader_is_dropped_silently(registry):
    # No reader connected — feed must not raise or block.
    await registry.create_passive_for_task("spec-3")
    registry.feed("spec-3", b"nobody is watching\r\n")
    state = registry.get_state("spec-3")
    assert state.write_fd is None  # open failed (ENXIO), bytes dropped
    await registry.reap_for_task("spec-3")


@pytest.mark.asyncio
async def test_feed_unknown_spec_is_noop(registry):
    registry.feed("does-not-exist", b"x")  # must not raise
