#!/usr/bin/env python3
"""Connect/first-token watchdog on the session enter (#816).

The coder + parallel session sites used ``async with client:`` with no timeout
on the enter. Entering the client spawns the claude CLI + its MCP stdio servers
(two launched via ``npx`` at connect); a cold/network-restricted runner can
stall that handshake with no first API call, and the session then burned
silently to the k8s build deadline (~3600s) -> empty patch. This was the
dominant empty-patch cause in the 2026-07-11 SWE-bench baseline (20/22).

``run_session_guarded`` bounds the enter with ``FIRST_TOKEN_TIMEOUT_SECONDS`` so
a stall becomes a fast retryable ``session_stall`` error instead of a hang.
"""

import asyncio

import agents.session as session
import pytest


class _HangingClient:
    """__aenter__ never returns within the watchdog window."""

    def __init__(self) -> None:
        self.exited = False

    async def __aenter__(self):  # noqa: D401 - test double
        await asyncio.sleep(30)
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False


class _FastClient:
    """__aenter__ returns promptly; records that __aexit__ ran."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False


@pytest.mark.asyncio
async def test_stalled_enter_returns_session_stall_fast(tmp_path, monkeypatch):
    # Tiny timeout so the test is fast; the client would otherwise hang 30s.
    monkeypatch.setattr(session, "FIRST_TOKEN_TIMEOUT_SECONDS", 0.05)
    client = _HangingClient()

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    status, response, error_info = await session.run_session_guarded(
        client, "prompt", tmp_path
    )
    elapsed = loop.time() - t0

    assert status == "error"
    assert response == ""
    assert error_info["type"] == "session_stall"
    # Converted a would-be 30s (deadline in prod) hang into a sub-second failover.
    assert elapsed < 5


@pytest.mark.asyncio
async def test_happy_path_runs_session_and_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "FIRST_TOKEN_TIMEOUT_SECONDS", 5.0)

    async def _fake_run(client, message, spec_dir, verbose=False, phase=None):
        return "complete", "done", {"usage": {}}

    monkeypatch.setattr(session, "run_agent_session", _fake_run)
    client = _FastClient()

    status, response, error_info = await session.run_session_guarded(
        client, "prompt", tmp_path
    )

    assert (status, response) == ("complete", "done")
    assert client.entered and client.exited  # enter + guaranteed __aexit__


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
