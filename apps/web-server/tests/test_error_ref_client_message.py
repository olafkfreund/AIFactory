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
now travels as ``InputRejectedError.client_message`` -- a plain string this
repo's validators write -- rather than as ``str(exc)``, which renders whatever
populated ``args``. That mechanism is only worth anything while the raise sites
keep their end of the bargain, so: rewrite ``services/url_safety.py``'s
``socket.gaierror`` branch to ``raise InputRejectedError(f"... {exc}")`` and
``test_a_resolver_failure_takes_the_reference_id_path`` goes red on the body.
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
        "server.routes.projects.load_projects",
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
async def test_a_resolver_failure_takes_the_reference_id_path() -> None:
    """A DNS failure's text is the resolver's, not ours. It must not cross.

    ``url_safety`` raises a plain ``ValueError`` here on purpose, so
    ``client_error`` logs it and returns a correlation id. Turn that raise into
    an ``InputRejectedError`` carrying ``str(exc)`` and this assertion fails --
    which is the point: the mechanism cannot police the raise sites, so a test
    does.
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
    assert RESOLVER_TEXT not in result["error"]
    assert "nope.invalid" not in result["error"]
    assert "reference" in result["error"]
