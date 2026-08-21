"""A credentialed git argv must never reach the log FILES (AIFactory sibling of
PFactory#576 / PFactory CodeQL 1696-1700).

`_inject_credential` embeds the PAT in the fetch URL, which becomes an argv
element. `_run_git` then logged `" ".join(args)` at DEBUG and interpolated
`' '.join(args)` into every `GitOperationError` message. Driving the REAL
pipeline (`setup_logging` -> log dir, `clone_or_update` with a credential) put
the token on THREE lines across two files:

    server.log  {"event": "[workspace] running: git clone -- https://oauth2:ghp_...@..."}
    server.log  {"event": "Clone failed [ref=...]: git clone -- https://oauth2:ghp_...",
                 "exception": "...ghp_..."}          <- the same record, twice over
    errors.log  (the same error_reference record, second handler)

That was the true count -- three, not one per call site, because
`error_reference` renders the message AND the exception. This fleet forwards
application logs off-host.

CodeQL flagged none of it here. It flagged the equivalent shape in PFactory
(alerts 1696-1700), whose fork of this file had already been hardened; this
one had not been, and reported clean.

The client response was never affected: `routes/projects.py` wraps the error in
`client_error`, which returns a reference id, and the JSON formatter
(AIFactory#1320) keeps the `exc_info` render inside a string field. Both are
verified below so a regression in either shows up here rather than silently.

Why FILE LINES and not `caplog`: `caplog` sees `LogRecord`s before the
handlers' formatter runs, so it cannot see what `exc_info` adds on the way to
disk -- which is two of the three leaking lines above.

Mutation check: restore `sanitize_log(" ".join(args))` in the DEBUG call and
`test_a_credential_bearing_argv_is_never_logged` goes red with the PAT on a
`server.log` line. That test drives `_run_git` with a token-bearing URL
DIRECTLY, and it has to: since #1362 `clone_or_update` no longer builds one,
so `test_credential_is_never_written_to_any_log_file` -- which goes through
`clone_or_update` -- stays green under that mutation and no longer guards the
logging property on its own. Two independent properties, two tests.

AIFactory#1362 converged this module on TFactory's fork: the token is no
longer an argv element at all (`GIT_ASKPASS` feeds it via `GIT_PASS`), so
`/proc/<pid>/cmdline` is clean too -- the residual #1356 could not reach,
because it only stopped the argv reaching the log. That property is pinned by
`test_token_is_absent_from_the_child_argv` below. The argv is STILL never
logged: it carries caller-controlled text (a URL, a branch) that has no
business on an off-host-forwarded log line whether or not it is secret.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import patch

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.error_ref import client_error  # noqa: E402
from server.logging_config import setup_logging  # noqa: E402
from server.services.project_workspace_service import (  # noqa: E402
    GitOperationError,
    _run_git,
    _safe_subcommand,
    clone_or_update,
)


# Assembled at import time rather than written as one literal: a realistic
# 40-char PAT literal trips the repo's gitleaks gate (generic-api-key, on
# entropy), and silencing a secret scanner to land a secret-leak test would be
# the wrong trade. The repeated word keeps the entropy low while the value is
# still PAT-shaped and unmistakable in a log line.
class _Spawn(NamedTuple):
    """One recorded ``create_subprocess_exec`` call: what we asked for, what
    the kernel published, and what env the child got."""

    argv: list[str]
    cmdline: bytes
    env: dict[str, str]


_SECRET = "ghp_" + "ARGVLEAKCANARY" * 3
# A closed local port fails immediately -- no network, no DNS.
_URL = "https://127.0.0.1:1/owner/repo.git"


@pytest.fixture
def log_lines(tmp_path: Path) -> Iterator[Callable[[], dict[str, list[str]]]]:
    """Drive the real pipeline into a temp log dir; yield a file-line reader."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    log_dir = tmp_path / "logs"
    setup_logging(log_level="DEBUG", log_dir=log_dir)

    def read() -> dict[str, list[str]]:
        for handler in logging.getLogger().handlers:
            handler.flush()
        return {
            f.name: f.read_text().splitlines() for f in sorted(log_dir.glob("*.log"))
        }

    try:
        yield read
    finally:
        for handler in logging.getLogger().handlers[:]:
            handler.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


@pytest.mark.asyncio
async def test_credential_is_never_written_to_any_log_file(
    tmp_path: Path, log_lines: Callable[[], dict[str, list[str]]]
) -> None:
    with pytest.raises(GitOperationError) as excinfo:
        await clone_or_update(
            git_url=_URL, root=tmp_path, slug="repo", credential=("oauth2", _SECRET)
        )

    assert _SECRET not in str(excinfo.value)

    files = log_lines()
    # Guard against a vacuous pass: if nothing was written, "absent" is
    # true and meaningless.
    assert any(
        "project_workspace_service" in line
        for lines in files.values()
        for line in lines
    ), f"no workspace log lines were written at all: {files}"

    leaks = [
        f"{name}:{i}: {line}"
        for name, lines in files.items()
        for i, line in enumerate(lines, 1)
        if _SECRET in line
    ]
    assert leaks == [], "credential written to log file(s):\n" + "\n".join(leaks)


@pytest.mark.asyncio
async def test_the_operation_is_still_identifiable(
    tmp_path: Path, log_lines: Callable[[], dict[str, list[str]]]
) -> None:
    """Withholding the argv must not make the log useless."""
    with pytest.raises(GitOperationError) as excinfo:
        await clone_or_update(
            git_url=_URL, root=tmp_path, slug="repo", credential=("oauth2", _SECRET)
        )

    assert "clone" in str(excinfo.value)
    assert "failed" in str(excinfo.value)
    lines = [
        line
        for group in log_lines().values()
        for line in group
        if "project_workspace_service" in line
    ]
    assert any("running: git clone" in line for line in lines), lines


def test_client_response_carries_a_reference_not_the_message(
    log_lines: Callable[[], dict[str, list[str]]],
) -> None:
    """`routes/projects.py` returns `client_error(...)` as the 400 detail. It
    must stay a reference id: this is the second sink the argv used to reach."""
    exc = GitOperationError(f"git clone -- https://oauth2:{_SECRET}@h/o/r.git failed")
    detail = client_error(logging.getLogger("test_workspace_argv"), "Clone failed", exc)

    assert _SECRET not in detail
    assert "reference" in detail

    # The reference id is only useful if an operator can find the record it
    # points at, so pin the correlation rather than just the redaction. Note
    # this exception's message is CONSTRUCTED to contain the secret, to
    # exercise `client_error` in isolation -- the real fix is that `_run_git`
    # no longer builds such a message, which the tests above prove.
    ref = detail.split("reference ")[1].rstrip(")")
    lines = [line for group in log_lines().values() for line in group]
    assert any(ref in line for line in lines), (ref, lines)


def test_unrecognised_subcommand_does_not_echo_argv_text() -> None:
    assert _safe_subcommand(["clone", f"https://oauth2:{_SECRET}@h/o/r.git"]) == "clone"
    assert _safe_subcommand([]) == "unknown"
    assert (
        _safe_subcommand([f"log\nCRITICAL:server.audit:forged {_SECRET}"]) == "unknown"
    )


@pytest.mark.asyncio
async def test_token_is_absent_from_the_child_argv(tmp_path: Path) -> None:
    """AIFactory#1362's own property: the token must not be in the child argv.

    This is what `GIT_ASKPASS` adds over #1356. #1356 stopped the credentialed
    argv reaching the LOG; the argv itself still carried the PAT and
    `/proc/<pid>/cmdline` is world-readable to every other uid on the host for
    the lifetime of the clone. Without this test the convergence could be
    called done by something that only MOVES the leak.

    Both forms of the check, on ONE real child process:

    * the recorded ``create_subprocess_exec`` args (what this module asked
      for), and
    * ``/proc/<pid>/cmdline`` (what the kernel actually published), read while
      the process is still alive.

    The remote is a real socket that accepts and never speaks, so git blocks
    in the HTTP exchange and the read is not racing the child's exit.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def read_cmdline(pid: int) -> bytes:
        """Sync helper: ASYNC240 forbids pathlib inside an async def."""
        return Path(f"/proc/{pid}/cmdline").read_bytes()

    real_exec = asyncio.create_subprocess_exec
    seen: list[_Spawn] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        proc = await real_exec(*args, **kwargs)
        seen.append(
            _Spawn(
                argv=[str(a) for a in args],
                cmdline=read_cmdline(proc.pid),
                env=dict(kwargs.get("env") or {}),
            )
        )
        return proc

    try:
        with (
            patch("asyncio.create_subprocess_exec", new=spy),
            pytest.raises(GitOperationError),
        ):
            await clone_or_update(
                git_url=f"https://127.0.0.1:{port}/owner/repo.git",
                root=tmp_path,
                slug="argv-probe",
                credential=("oauth2", _SECRET),
                timeout_seconds=3,
            )
    finally:
        listener.close()

    assert seen, "no child process was spawned"

    # Guard against a vacuous pass: the credential must actually have been in
    # play on this call, just by a route that isn't argv.
    assert any(spawn.env.get("GIT_PASS") == _SECRET for spawn in seen), (
        "the token never reached GIT_PASS -- this test would pass vacuously"
    )

    argv_leaks = [s.argv for s in seen if any(_SECRET in a for a in s.argv)]
    assert argv_leaks == [], (
        f"token present in create_subprocess_exec args: {argv_leaks}"
    )

    proc_leaks = [
        s.cmdline.decode("utf-8", "replace")
        for s in seen
        if _SECRET.encode() in s.cmdline
    ]
    assert proc_leaks == [], f"token present in /proc/<pid>/cmdline: {proc_leaks}"


@pytest.mark.asyncio
async def test_a_credential_bearing_argv_is_never_logged(
    tmp_path: Path, log_lines: Callable[[], dict[str, list[str]]]
) -> None:
    """The LOGGING property, pinned independently of what builds the argv.

    `clone_or_update` no longer puts a credential in a URL (#1362), so the
    sibling test above cannot fail when the DEBUG line starts printing argv
    again -- there is nothing secret in the argv it produces. This one hands
    `_run_git` a token-bearing URL directly, so the guard from #1356 stays
    red-able on its own: the argv must not reach a log line, whatever ends up
    in it.
    """
    url = f"https://oauth2:{_SECRET}@127.0.0.1:1/owner/repo.git"

    with pytest.raises(GitOperationError) as excinfo:
        await _run_git(
            ["clone", "--", url, str(tmp_path / "dest")], cwd=tmp_path, timeout=10
        )

    assert _SECRET not in str(excinfo.value)

    files = log_lines()
    # Guard against a vacuous pass: if the pipeline wrote nothing, "the secret
    # is absent" is true and meaningless.
    assert any(
        "project_workspace_service" in line
        for lines in files.values()
        for line in lines
    ), f"no workspace log lines were written at all: {files}"

    leaks = [
        f"{name}:{i}: {line}"
        for name, lines in files.items()
        for i, line in enumerate(lines, 1)
        if _SECRET in line
    ]
    assert leaks == [], "credential written to log file(s):\n" + "\n".join(leaks)
