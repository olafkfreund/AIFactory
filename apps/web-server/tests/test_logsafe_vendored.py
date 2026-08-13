"""The vendored CWE-117 sanitizer, and one real call site that depends on it.

These are mutation tests, not shape tests. Delete ``.replace("\\n", "\\\\n")``
from ``apps/web-server/factory_common/logsafe.py`` and the two forge tests below
go red, because they drive a REAL ``logging`` handler and count the records that
came out -- not because a string compared differently.
"""

from __future__ import annotations

import logging

import pytest
from factory_common.logsafe import sanitize_log
from server.services.task_status import read_plan

# What an attacker puts in a task id / spec id / filename to write their own
# line into the log the incident response is read from.
FORGERY = "spec-001\nWARNING:server.audit:api key revoked by admin"
FORGED_LINE = "WARNING:server.audit:api key revoked by admin"


class _Capture(logging.Handler):
    """A real handler that models a line-oriented log sink.

    ``splitlines()`` is the whole point and must not be simplified away to
    ``append``. Python's logging always emits exactly ONE ``LogRecord`` no matter
    how many newlines the message carries, so counting records can never see the
    forgery -- an early version of this test passed against a sanitizer with the
    newline barrier deleted. What an attacker forges is a LINE in the log FILE,
    which is what a file handler, journald, and every shipper downstream read.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.extend(self.format(record).splitlines())


@pytest.fixture
def capture() -> _Capture:
    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    return handler


def _attach(handler: logging.Handler, name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def test_crlf_and_controls_are_escaped_not_stripped() -> None:
    assert sanitize_log("a\nb") == "a\\nb"
    assert sanitize_log("a\r\nb") == "a\\nb"
    assert sanitize_log("a\rb") == "a\\rb"
    assert sanitize_log("a\x00b") == "a\\x00b"
    # Preserved on purpose: tab is not a record separator, and mangling unicode
    # would break every non-English identifier the fleet logs.
    assert sanitize_log("a\tbé") == "a\tbé"
    # Non-strings are stringified, so wrapping an int or a Path is safe.
    assert sanitize_log(7) == "7"


def test_sanitized_value_cannot_forge_a_record(capture: _Capture) -> None:
    logger = _attach(capture, "server.tests.logsafe")

    logger.info("[StartTask] task_id: %s", sanitize_log(FORGERY))

    assert len(capture.lines) == 1, capture.lines
    assert FORGED_LINE not in capture.lines
    # The payload is still readable and greppable -- escaped, not deleted.
    assert "api key revoked by admin" in capture.lines[0]


def test_a_real_call_site_cannot_forge_a_record(
    capture: _Capture, tmp_path
) -> None:
    """server.services.task_status.read_plan logs a caller-supplied path.

    The path is derived from a task id that arrives on the URL, so this is the
    live shape of the bug, not a reconstruction of it.
    """
    _attach(capture, "server.services.task_status")
    hostile = tmp_path / "plan.json"
    hostile.write_text("{ not json")

    read_plan(
        type(hostile)(
            str(hostile).replace(
                "plan.json",
                "plan.json\nWARNING:server.audit:api key revoked by admin",
            )
        )
    )

    assert capture.lines, "read_plan logged nothing; the test is not exercising it"
    assert FORGED_LINE not in capture.lines
    assert len(capture.lines) == 1, capture.lines


def test_the_numeric_conversions_still_emit(capture: _Capture) -> None:
    """A sanitized value is a str, so a leftover %d raises at EMIT time.

    read_plan's JSONDecodeError branch formats four values that used to be %d.
    A TypeError here would be invisible to ruff and to mypy; only running the
    call catches it, which is what this does.
    """
    logger = _attach(capture, "server.services.task_status")
    logging.raiseExceptions = True  # surface a formatting error instead of stderr

    logger.error(
        "%s is not valid JSON at line %s column %s (char %s): %s",
        sanitize_log("plan.json"),
        sanitize_log(3),
        sanitize_log(11),
        sanitize_log(42),
        sanitize_log("Expecting value"),
    )

    assert capture.lines == [
        "ERROR:server.services.task_status:plan.json is not valid JSON at line 3 "
        "column 11 (char 42): Expecting value"
    ]
