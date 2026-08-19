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
`test_credential_is_never_written_to_any_log_file` goes red with the PAT on a
`server.log` line.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.error_ref import client_error  # noqa: E402
from server.logging_config import setup_logging  # noqa: E402
from server.services.project_workspace_service import (  # noqa: E402
    GitOperationError,
    _safe_subcommand,
    clone_or_update,
)

# Assembled at import time rather than written as one literal: a realistic
# 40-char PAT literal trips the repo's gitleaks gate (generic-api-key, on
# entropy), and silencing a secret scanner to land a secret-leak test would be
# the wrong trade. The repeated word keeps the entropy low while the value is
# still PAT-shaped and unmistakable in a log line.
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
