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
import re
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.error_ref import client_error, error_reference
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

    def emit(self, record: logging.LogRecord) -> None:
        self.text += self.format(record) + "\n"


@pytest.fixture
def logs() -> _Capture:
    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger = logging.getLogger("server.tests.error_ref")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler.logger = logger  # type: ignore[attr-defined]
    return handler


def test_the_client_message_carries_no_exception_detail(logs: _Capture) -> None:
    message = client_error(logs.logger, "Failed to create the pull request", BOOM)

    _assert_no_leak(message)
    assert message.startswith("Failed to create the pull request")
    assert REF.search(message), message


def test_the_detail_is_recoverable_from_the_log_under_that_id(
    logs: _Capture,
) -> None:
    message = client_error(logs.logger, "Failed to create the pull request", BOOM)
    ref = REF.search(message).group()

    assert f"[ref={ref}]" in logs.text
    # Support has to be able to answer "what actually happened".
    assert CREDENTIALS_FILE in logs.text
    assert INTERNAL_HOST in logs.text


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
    with patch("subprocess.run", side_effect=OSError(f"cannot exec {CREDENTIALS_FILE}")):
        result = gh.run_gh_command(["repo", "view"])

    assert result["success"] is False
    _assert_no_leak(result["error"])
    assert REF.search(result["error"]), result["error"]


class _FailingPtyManager:
    def create_session(self, **_kwargs: object) -> None:
        raise BOOM


def test_a_real_route_response_body_leaks_nothing(tmp_path) -> None:
    """POST /api/terminals, with the PTY manager failing the way it does in prod.

    Asserted on the RESPONSE BODY, not on the status code: a 503 with the
    hostname in `detail` is exactly the bug this closes.
    """
    app = FastAPI()
    app.include_router(terminal.router, prefix="/api/terminals")

    with patch.object(terminal, "get_pty_manager", return_value=_FailingPtyManager()):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/terminals", json={"cols": 80, "rows": 24, "cwd": str(tmp_path)}
        )

    assert response.status_code == 503, response.text
    _assert_no_leak(response.text)
    assert REF.search(response.text), response.text
