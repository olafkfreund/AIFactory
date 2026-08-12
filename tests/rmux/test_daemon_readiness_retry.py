"""A daemon still coming up must not fail the command (#1233).

The R0b round-trip failed a BLOCKING check on a Helm-chart-only PR:

    RmuxError: rmux list-sessions failed (rc=1):
        i/o error: Connection reset by peer (os error 104)

`new_session` returns as soon as rmux forks the daemon, so the next command can
reach a socket that is BOUND but not yet serving — the connect succeeds and is
immediately dropped. `_run` already recognised two spellings of "daemon not
reachable" (`no server running`, `error connecting to ... no such file`) but not
this third one, so it fell through to a generic RmuxError.

These tests reproduce that condition DETERMINISTICALLY by controlling what the
rmux binary says, rather than waiting for the race. 45 real runs of the
integration round-trip — serial, whole-directory, and under CPU load — did not
reproduce it locally, which is exactly why a timing-dependent test would be the
wrong instrument here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from server.rmux.wrapper import (
    RmuxDaemonError,
    RmuxError,
    RmuxSessionError,
    RmuxWrapper,
)

_RESET = "i/o error: Connection reset by peer (os error 104)"


def _wrapper(tmp_path: Path) -> RmuxWrapper:
    return RmuxWrapper(socket_dir=tmp_path)


def _fake_run_once(script: list[str | None]):
    """Return a `_run_once` that yields each scripted stderr in turn.

    A `None` entry means "this call succeeds".
    """
    calls: list[tuple[str, ...]] = []

    async def _run_once(*args: str, **_kwargs: object) -> str:
        calls.append(args)
        stderr = script[min(len(calls) - 1, len(script) - 1)]
        if stderr is None:
            return "ok"
        lower = stderr.lower()
        if "connection reset" in lower or "no server" in lower:
            raise RmuxDaemonError(stderr)
        raise RmuxError(stderr)

    return _run_once, calls


def test_a_connection_reset_is_classified_as_daemon_not_ready(tmp_path: Path) -> None:
    """The third spelling must reach RmuxDaemonError, not generic RmuxError.

    Generic RmuxError is what failed the round-trip: the retry below can only
    help if the condition is classified as a daemon problem in the first place.

    This drives the REAL `_run_once` against a stub binary that prints the
    message and exits 1 — the first version stubbed `_run_once` itself and so
    asserted nothing about the classifier. Deleting the classification left it
    green, which is how that was caught.
    """
    fake_bin = tmp_path / "rmux-stub"
    fake_bin.write_text(f'#!/bin/sh\necho "{_RESET}" >&2\nexit 1\n')
    fake_bin.chmod(0o755)

    w = RmuxWrapper(socket_dir=tmp_path, rmux_bin=str(fake_bin))
    w._DAEMON_READY_TIMEOUT_S = 0.0  # no retry: we are testing classification

    with pytest.raises(RmuxDaemonError):
        asyncio.run(w._run("list-sessions", capture=True))


def test_the_command_is_retried_while_the_daemon_comes_up(tmp_path: Path) -> None:
    """Reset, reset, then success — the caller must see success."""
    w = _wrapper(tmp_path)
    run_once, calls = _fake_run_once([_RESET, _RESET, None])
    w._run_once = run_once

    assert asyncio.run(w._run("list-sessions", capture=True)) == "ok"
    assert len(calls) == 3, f"expected two retries then success, got {len(calls)} calls"


def test_the_retry_is_bounded_and_still_raises(tmp_path: Path) -> None:
    """A daemon that never comes up must still fail, and quickly.

    Without this the fix trades a flaky red for a hang, which is worse: a
    genuinely broken rmux would look like a slow test rather than an error.
    """
    w = _wrapper(tmp_path)
    run_once, calls = _fake_run_once([_RESET])
    w._run_once = run_once
    w._DAEMON_READY_TIMEOUT_S = 0.2

    with pytest.raises(RmuxDaemonError):
        asyncio.run(w._run("list-sessions", capture=True))
    assert len(calls) > 1, "it should have retried at least once before giving up"


def test_a_real_error_is_not_retried(tmp_path: Path) -> None:
    """Only daemon-not-ready retries. A missing session fails at once.

    Retrying a genuine error just makes it slower to report, and would mask a
    real regression behind a two-second delay.
    """
    w = _wrapper(tmp_path)

    async def _run_once(*_args: str, **_kwargs: object) -> str:
        raise RmuxSessionError("can't find session: nope")

    w._run_once = _run_once

    with pytest.raises(RmuxSessionError):
        asyncio.run(w._run("kill-session", capture=True))


def test_swallow_no_server_skips_the_retry(tmp_path: Path) -> None:
    """`ensure_daemon`'s probe must stay fast.

    It passes `swallow_no_server=True`, meaning "no daemon is a fine answer" —
    so waiting two seconds for one would add that to every probe for nothing.
    """
    w = _wrapper(tmp_path)
    run_once, calls = _fake_run_once([None])
    w._run_once = run_once

    assert asyncio.run(w._run("list-sessions", swallow_no_server=True)) == "ok"
    assert len(calls) == 1
