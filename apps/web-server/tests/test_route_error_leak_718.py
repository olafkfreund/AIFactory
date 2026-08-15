"""Foreign exceptions must not reach a response body (Factory#718).

Every test here drives a REAL route handler and asserts on the ``detail`` the
caller receives. That framing is the point: the interesting direction is not
"a deliberate rejection still shows its message" -- that passed before the
change too, and a handler that echoed everything would pass it as well. The
test that proves anything is a FOREIGN exception reaching the SAME handler and
coming back as a reference id.

So each handler is exercised twice where it has both paths:

* the repo's own ``InputRejectedError`` -> its sentence, verbatim;
* something the stdlib or a library raised -> ``context (reference <id>)``,
  with none of the original text.

The sentinels below are strings nothing legitimate produces. Asserting their
ABSENCE is what makes these leak tests rather than smoke tests -- an assertion
that the body merely "contains a reference" would still pass if the body
carried the leak as well.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from server.error_ref import InputRejectedError, client_error
from server.mcp_remote import auth as mcp_auth
from server.routes import files, inbox
from server.services import inbox_service

# An absolute server path of the kind an OSError renders. If this appears in a
# response body, a handler is forwarding exception text again.
DISCLOSED_PATH = "/srv/aifactory/private/SENTINEL-do-not-disclose.pem"
# Text only a third-party library would write.
LIBRARY_DETAIL = "SENTINEL-library-internal-detail"

_REFERENCE = re.compile(r"\(reference [0-9a-f]{12}\)")


def _detail(exc_info: pytest.ExceptionInfo[HTTPException]) -> str:
    return str(exc_info.value.detail)


def _assert_redacted(detail: str, *leaked: str) -> None:
    """The body carries a reference id and none of the original text."""
    assert _REFERENCE.search(detail), f"no reference id in {detail!r}"
    for text in leaked:
        assert text not in detail, f"{text!r} leaked into {detail!r}"


# ─── files.py: the filesystem routes ─────────────────────────────────────


@pytest.mark.asyncio
async def test_write_file_does_not_echo_an_oserror(tmp_path: Path) -> None:
    """An OSError names the absolute path it failed on. That is the leak."""
    target = tmp_path / "a.txt"

    with (
        patch.object(files, "resolve_path", return_value=target),
        patch.object(
            Path,
            "write_text",
            side_effect=PermissionError(13, "denied", DISCLOSED_PATH),
        ),
        patch.object(Path, "mkdir"),
        pytest.raises(HTTPException) as exc_info,
    ):
        await files.write_file(
            project_id="p1", path="a.txt", file_data=files.FileWrite(content="x")
        )

    assert exc_info.value.status_code == 500
    _assert_redacted(_detail(exc_info), DISCLOSED_PATH, "denied")


@pytest.mark.asyncio
async def test_delete_does_not_echo_an_oserror(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"

    with (
        patch.object(files, "resolve_path", return_value=target),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "is_dir", return_value=False),
        patch.object(Path, "unlink", side_effect=OSError(16, "busy", DISCLOSED_PATH)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await files.delete_file(project_id="p1", path="a.txt")

    _assert_redacted(_detail(exc_info), DISCLOSED_PATH, "busy")


# ─── the InputRejectedError contract, both directions ────────────────────


def test_a_rejected_field_returns_its_own_sentence() -> None:
    """The passthrough half. Worth pinning, but it proves nothing on its own."""

    rejected = InputRejectedError("Invalid base: must be a plain git ref")
    assert (
        client_error(logging.getLogger(__name__), "ignored", rejected)
        == "Invalid base: must be a plain git ref"
    )


def test_a_foreign_valueerror_does_not_get_the_passthrough() -> None:
    """The half that matters: a plain ValueError is NOT caller-authored.

    ``InputRejectedError`` subclasses ``ValueError`` so existing handlers keep
    catching it. That inheritance is what makes this test necessary -- it would
    be easy to write the check as ``isinstance(exc, ValueError)`` and hand back
    everything the stdlib raises.
    """

    foreign = ValueError(f"could not parse {DISCLOSED_PATH}")
    detail = client_error(logging.getLogger(__name__), "Request failed", foreign)
    _assert_redacted(detail, DISCLOSED_PATH)


def test_input_rejected_is_still_caught_by_except_valueerror() -> None:
    """Pins the inheritance the conversions rely on.

    Several call sites catch ``ValueError`` and now receive
    ``InputRejectedError`` from their service layer. If this subclassing were
    ever dropped, those handlers would stop catching and the exception would
    escape as a 500 -- silently, because every one of them would still compile.
    """
    try:
        raise InputRejectedError("rejected")
    except ValueError as exc:
        assert isinstance(exc, InputRejectedError)
        assert exc.client_message == "rejected"
    else:  # pragma: no cover - the except above always runs
        pytest.fail("InputRejectedError was not caught by `except ValueError`")


# ─── inbox: a repo-owned type that launders, and one that does not ───────


@pytest.mark.asyncio
async def test_inbox_rejection_text_survives() -> None:
    """ "Message text must not be empty" is developer-written and stays."""

    with (
        patch.object(
            inbox, "_resolve_task", return_value=("p", "s", Path("/t"), Path("/t"))
        ),
        patch.object(inbox, "_inbox_target_spec_dir", return_value=Path("/t")),
        patch.object(
            inbox_service,
            "enqueue",
            side_effect=inbox_service.InboxTextRejectedError(
                "Message text must not be empty"
            ),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await inbox.enqueue_inbox_message(
            task_id="t1",
            message=inbox.InboxMessageCreate(text="x", recipient="r", sender="s"),
        )

    assert _detail(exc_info) == "Message text must not be empty"


@pytest.mark.asyncio
async def test_inbox_path_bearing_error_is_redacted() -> None:
    """The same handler, an InboxError that interpolated an absolute path.

    This is the pair that matters: one repo-owned exception type, two very
    different provenances, and only the marked one crosses the boundary.
    """

    with (
        patch.object(
            inbox, "_resolve_task", return_value=("p", "s", Path("/t"), Path("/t"))
        ),
        patch.object(inbox, "_inbox_target_spec_dir", return_value=Path("/t")),
        patch.object(
            inbox_service,
            "enqueue",
            side_effect=inbox_service.InboxError(
                f"Failed to read inbox {DISCLOSED_PATH}: {LIBRARY_DETAIL}"
            ),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await inbox.enqueue_inbox_message(
            task_id="t1",
            message=inbox.InboxMessageCreate(text="x", recipient="r", sender="s"),
        )

    _assert_redacted(_detail(exc_info), DISCLOSED_PATH, LIBRARY_DETAIL)


# ─── mcp auth: the one message that described OUR state ──────────────────


def test_mcp_auth_no_longer_names_the_database() -> None:
    """An unauthenticated caller learns that auth failed, not why.

    ``MCPAuthError("Database session not available")`` answered a 401 with our
    internal state. Every other message in that module is about the caller's
    own credential and is deliberately still verbatim.
    """

    source = Path(mcp_auth.__file__).read_text(encoding="utf-8")
    assert "Database session not available" not in source
    assert isinstance(mcp_auth.MCPAuthError("x"), InputRejectedError)
