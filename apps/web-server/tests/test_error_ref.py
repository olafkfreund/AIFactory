"""CWE-209: what an exception is allowed to tell the caller.

Nothing, is the answer. The caller gets a generic sentence and a correlation id;
the class name, the message, the traceback, and everything they name -- absolute
paths, internal hostnames, ports, which env vars are unset -- go to the log under
that id.

These are mutation tests. Make ``error_reference`` return ``str(exc)`` instead of
the id and every test below that asserts on a RESPONSE BODY goes red.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.error_ref import InputRejectedError, client_error, error_reference
from server.routes import terminal
from server.services import gh

REF = re.compile(r"\b[0-9a-f]{12}\b")

# One exception carrying every kind of internal detail these handlers leak in
# production: an absolute path, an internal hostname, a port, and a class name.
# Named CREDENTIALS_FILE rather than SECRET_PATH because it is the PATH a
# credentials file would sit at, not a credential -- and ruff's S105 keys off
# the variable name.
CREDENTIALS_FILE = "/etc/aifactory/credentials.yaml"
INTERNAL_HOST = "postgres-primary.aifactory.svc.cluster.local"
INTERNAL_PORT = "5432"
BOOM = RuntimeError(
    f"could not connect to {INTERNAL_HOST}:{INTERNAL_PORT}: "
    f"no password supplied and {CREDENTIALS_FILE} is unreadable"
)
LEAKS = (CREDENTIALS_FILE, INTERNAL_HOST, INTERNAL_PORT, "RuntimeError", "Traceback")


def _assert_no_leak(text: str) -> None:
    for fragment in LEAKS:
        assert fragment not in text, f"leaked {fragment!r} in: {text!r}"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.text = ""
        self.logger = logging.getLogger("server.tests.error_ref")
        self.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        self.logger.handlers = [self]
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    def emit(self, record: logging.LogRecord) -> None:
        self.text += self.format(record) + "\n"


@pytest.fixture
def logs() -> _Capture:
    return _Capture()


def _ref_in(message: str) -> str:
    match = REF.search(message)
    assert match is not None, f"no correlation id in: {message!r}"
    return match.group()


def test_the_client_message_carries_no_exception_detail(logs: _Capture) -> None:
    message = client_error(logs.logger, "Failed to create the pull request", BOOM)

    _assert_no_leak(message)
    assert message.startswith("Failed to create the pull request")
    _ref_in(message)


def test_the_detail_is_recoverable_from_the_log_under_that_id(
    logs: _Capture,
) -> None:
    message = client_error(logs.logger, "Failed to create the pull request", BOOM)
    ref = _ref_in(message)

    assert f"[ref={ref}]" in logs.text
    # Support has to be able to answer "what actually happened".
    assert CREDENTIALS_FILE in logs.text
    assert INTERNAL_HOST in logs.text


def test_a_rejected_field_is_still_told_to_the_caller(logs: _Capture) -> None:
    """The fix must not swallow validation errors.

    "Invalid baseBranch: must be a plain git ref" is developer-written and names
    only the field the caller sent. Hiding it behind a reference id turns a
    fixable 400 into a support ticket, and a test in tests/test_argv_safety.py
    caught exactly that regression.
    """
    message = client_error(
        logs.logger,
        "get commits preview failed",
        InputRejectedError("Invalid baseBranch"),
    )

    assert message == "Invalid baseBranch"
    assert REF.search(message) is None
    assert logs.text == "", "a rejected field is the validator working, not an incident"


def test_two_failures_get_different_ids(logs: _Capture) -> None:
    a = error_reference(logs.logger, "x", BOOM)
    b = error_reference(logs.logger, "x", BOOM)
    assert a != b


def test_a_newline_in_the_exception_cannot_forge_a_log_record(
    logs: _Capture,
) -> None:
    """The CWE-209 fix must not become a CWE-117 hole.

    An exception's text is frequently attacker-supplied (a filename, a URL, a
    subprocess's stderr), so the summary line is sanitized on its way to the log.
    """
    forged = "WARNING:server.audit:api key revoked by admin"
    error_reference(logs.logger, "boom", ValueError(f"nope\n{forged}"))

    summary = logs.text.splitlines()[0]
    assert forged in summary, "the payload must stay readable, escaped not stripped"
    assert "\\n" in summary


def test_the_gh_helper_does_not_hand_back_the_exception_text() -> None:
    """The root of the flow: every `result.get("error")` sink reads this dict."""
    with patch(
        "subprocess.run", side_effect=OSError(f"cannot exec {CREDENTIALS_FILE}")
    ):
        result = gh.run_gh_command(["repo", "view"])

    assert result["success"] is False
    _assert_no_leak(result["error"])
    _ref_in(result["error"])


class _FailingPtyManager:
    def create_session(self, **_kwargs: object) -> None:
        raise BOOM


def test_a_real_route_response_body_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/terminals, with the PTY manager failing the way it does in prod.

    Asserted on the RESPONSE BODY, not on the status code: a 503 with the
    hostname in `detail` is exactly the bug this closes.

    #1278 confines `cwd` to the browsable roots, so a bare tmp_path is refused
    with 403 before the PTY manager is reached and the 503 this test is about
    never happens. `APP_FILE_BROWSE_ROOTS` is the documented operator escape
    hatch for exactly this — a deployment whose code lives off $HOME — so the
    test uses it rather than weakening the confinement. Two roots, because with
    one "confines to the configured set" and "confines to the first entry" are
    the same observation.
    """
    monkeypatch.setenv(
        "APP_FILE_BROWSE_ROOTS", f"{tmp_path.parent}{os.pathsep}{tmp_path}"
    )

    app = FastAPI()
    app.include_router(terminal.router, prefix="/api/terminals")

    with patch.object(terminal, "get_pty_manager", return_value=_FailingPtyManager()):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/terminals", json={"cols": 80, "rows": 24, "cwd": str(tmp_path)}
        )

    assert response.status_code == 503, response.text
    _assert_no_leak(response.text)
    _ref_in(response.text)
