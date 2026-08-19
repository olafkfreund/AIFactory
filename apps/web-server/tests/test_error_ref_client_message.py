"""What crosses the handler boundary, asserted on the RESPONSE BODY.

``client_error`` has two outcomes and the difference between them is the whole
of CWE-209 in this server:

* a rejected field -> the validator's own sentence, verbatim, because
  "Invalid baseBranch: must be a plain git ref" is a fixable 400 and a
  correlation id would turn it into a support ticket;
* anything else -> a correlation id, because the text was written by the
  stdlib or a library and routinely names a path on our disk.

Both are asserted here through a route, on the dict the caller actually
receives. Testing ``client_error``'s return value instead would pass just as
happily while the route dropped it on the floor.

The second test is also the mutation check for the mechanism. The safe sentence
travels as ``InputRejectedError.client_message`` -- a plain string this repo's
validators write -- rather than as ``str(exc)``, which renders whatever
populated ``args``. That mechanism is only worth anything while the raise sites
keep their end of the bargain, so: put the ``socket.gaierror`` back into
``factory_common/url_safety.py``'s message (``raise InputRejectedError(f"cannot
resolve host {host!r}: {exc}")``, which is what the deleted web-server fork
did) and ``test_a_resolver_failure_does_not_leak_the_resolver_text`` goes red on
the body.

What changed in #1361: the resolve failure used to raise a plain ``ValueError``
carrying the resolver's text, so this server hid it behind a correlation id
while ``apps/backend`` -- which interpolates the same message into text the
agent reads back -- did not. The canonical now drops the resolver text at the
raise site and marks the message safe, so the fix holds for every consumer
rather than for whichever handler happened to be careful. The assertion that
matters is unchanged and is the first one: the resolver's wording must not
cross.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest
from server.routes import changelog, git

# Nothing legitimate says this. If it reaches a response body, some raise site
# started forwarding a runtime exception's text.
RESOLVER_TEXT = "SENTINEL-resolver-detail-Name-or-service-not-known"


@pytest.mark.asyncio
async def test_a_rejected_field_still_returns_its_own_message() -> None:
    """The passthrough exists for this. It must survive the mechanism change."""
    with patch(
        "server.project_registry.load_projects",
        # Never touched: the validator rejects the ref before git is spawned.
        return_value={"p1": {"path": "/nonexistent/project"}},
    ):
        result = await changelog.get_commits_preview(
            projectId="p1",
            request=changelog.CommitsPreviewRequest(
                mode="branch-diff",
                options={
                    # `git log --output=<file>` is an arbitrary file write, which
                    # is why a ref may not begin with '-'.
                    "baseBranch": "--output=pwned",
                    "compareBranch": "HEAD",
                },
            ),
        )

    assert result["success"] is False
    # Verbatim, and it names the field the caller got wrong.
    assert result["error"] == "Invalid baseBranch: must be a plain git ref"
    assert "reference" not in result["error"]


@pytest.mark.asyncio
async def test_a_resolver_failure_does_not_leak_the_resolver_text() -> None:
    """A DNS failure's text is the resolver's, not ours. It must not cross.

    The guard drops it at the raise site (Factory#831) and keeps it on
    ``__cause__``. Re-interpolate it -- ``InputRejectedError(f"... {exc}")``, the
    shape the deleted fork had -- and this goes red: the mechanism cannot police
    the raise sites, so a test does.

    The HOST does cross, and should: it is the string the caller just sent, so
    "cannot resolve host 'nope.invalid'" is a fixable 400 rather than a support
    ticket. That is the whole distinction ``InputRejectedError`` marks.
    """
    with (
        patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, RESOLVER_TEXT)),
        patch.object(git, "build_no_redirect_opener") as opener,
    ):
        result = await git.pull_ollama_model(
            git.PullModelRequest(modelName="x", baseUrl="http://nope.invalid:11434")
        )

    opener.assert_not_called()
    assert result["success"] is False
    # The one that matters: nothing the resolver wrote.
    assert RESOLVER_TEXT not in result["error"]
    assert "Errno" not in result["error"]
    # Developer-written, and it names the field the caller got wrong.
    assert result["error"] == "cannot resolve host 'nope.invalid'"
    assert "reference" not in result["error"]
