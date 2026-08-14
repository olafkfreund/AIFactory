"""CWE-209 / CWE-117 on the settings routes (AIFactory#1301).

``server/routes/settings.py`` held 17 ``py/stack-trace-exposure`` alerts and 2
``py/log-injection`` alerts -- the largest single concentration in the repo. The
handlers did ``return {"success": False, "error": str(e)}``, and that string is a
response body: an ``httpx.ConnectError`` names the host and port it could not
reach, an ``OSError`` names a path on our disk, and several of these routes are
reachable with no project scope at all.

These are MUTATION tests, and they assert the reason, not just the failure. Make
``server.error_ref.error_reference`` return ``str(exc)`` and every assertion
below that reads a RESPONSE BODY goes red naming the fragment that escaped.
Delete the ``sanitize_log`` call in ``list_openai_compat_models`` and the
log-forging test goes red instead.
"""

from __future__ import annotations

import json
import logging
import re
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.routes import settings as settings_routes

REF = re.compile(r"\b[0-9a-f]{12}\b")

# One exception carrying every kind of internal detail these handlers leaked:
# an internal hostname, a port, an absolute path, and a class name. Named
# CREDENTIALS_FILE rather than SECRET_PATH because it is the PATH a credentials
# file would sit at, not a credential -- ruff's S105 keys off the name.
CREDENTIALS_FILE = "/etc/aifactory/credentials.yaml"
INTERNAL_HOST = "ollama.aifactory.svc.cluster.local"
INTERNAL_PORT = "11434"
BOOM = ConnectionRefusedError(
    f"[Errno 111] Connection refused to {INTERNAL_HOST}:{INTERNAL_PORT} "
    f"while reading {CREDENTIALS_FILE}"
)
LEAKS = (
    CREDENTIALS_FILE,
    INTERNAL_HOST,
    INTERNAL_PORT,
    "ConnectionRefusedError",
    "Errno 111",
    "Traceback",
)


def _assert_no_leak(text: str) -> None:
    for fragment in LEAKS:
        assert fragment not in text, f"leaked {fragment!r} in response body: {text!r}"


def _ref_in(text: str) -> str:
    match = REF.search(text)
    assert match is not None, f"no correlation id in: {text!r}"
    return match.group()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(settings_routes.router, prefix="/api/settings")
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/settings/ollama/models", None),
        ("get", "/api/settings/openai-compat/models", None),
        ("post", "/api/settings/openai-compat/test", {"baseUrl": "http://x:1"}),
        ("post", "/api/settings/ollama/pull", {"modelName": "llama3"}),
        (
            "post",
            "/api/settings/ollama/test",
            {"ollamaBaseUrl": "http://x:1", "modelName": "llama3"},
        ),
        (
            "post",
            "/api/settings/api-profiles/test",
            {"baseUrl": "https://api.example.com", "apiKey": "sk-test"},
        ),
        (
            "post",
            "/api/settings/api-profiles/discover-models",
            {"baseUrl": "https://api.example.com", "apiKey": "sk-test"},
        ),
    ],
)
def test_an_outbound_failure_tells_the_caller_nothing_internal(
    client: TestClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    """Every route that reaches the network, failing the way it fails in prod.

    Asserted on the RESPONSE BODY, not the status code: a 200 with
    ``{"error": "Connection refused to ollama.aifactory.svc..."}`` is exactly
    the bug this closes.
    """
    with patch.object(settings_routes, "assert_safe_outbound_url", side_effect=BOOM):
        response = client.request(method.upper(), path, json=body)

    _assert_no_leak(response.text)
    _ref_in(response.text)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/settings/claude-profiles/active", {"profileId": "p1"}),
        ("post", "/api/settings/claude-profiles/p1/initialize", None),
        ("post", "/api/settings/api-profiles/active", {"profileId": "p1"}),
        ("delete", "/api/settings/api-profiles/p1", None),
        ("post", "/api/settings/retry-with-profile", {"profileId": "p1"}),
    ],
)
def test_a_store_failure_tells_the_caller_nothing_internal(
    client: TestClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    """The profile routes, with the JSON store on disk unreadable."""
    with (
        patch.object(settings_routes, "load_profiles", side_effect=BOOM),
        patch.object(settings_routes, "load_api_profiles", side_effect=BOOM),
    ):
        response = client.request(method.upper(), path, json=body)

    _assert_no_leak(response.text)
    _ref_in(response.text)


def test_the_auto_switch_write_failure_tells_the_caller_nothing_internal(
    client: TestClient,
) -> None:
    with patch.object(settings_routes, "_write_json_store", side_effect=BOOM):
        response = client.patch("/api/settings/auto-switch", json={"enabled": True})

    _assert_no_leak(response.text)
    _ref_in(response.text)


def test_the_detail_is_recoverable_from_the_log_under_that_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The redaction must not cost support the answer to "what happened"."""
    with (
        caplog.at_level(logging.WARNING, logger=settings_routes.__name__),
        patch.object(settings_routes, "assert_safe_outbound_url", side_effect=BOOM),
    ):
        response = client.get("/api/settings/ollama/models")

    ref = _ref_in(response.text)
    logged = caplog.text
    assert f"[ref={ref}]" in logged
    assert INTERNAL_HOST in logged
    assert CREDENTIALS_FILE in logged


def test_a_newline_in_the_base_url_cannot_forge_a_log_record(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """py/log-injection: the caller-controlled URL is interpolated into a log line.

    The CWE-209 fix must not become a CWE-117 hole, so the URL goes through
    ``factory_common.logsafe.sanitize_log`` -- escaped, not stripped, so the
    payload stays readable to whoever reads the log.
    """
    forged = "WARNING:server.audit:api key revoked by admin"
    with (
        caplog.at_level(logging.WARNING, logger=settings_routes.__name__),
        patch.object(settings_routes, "assert_safe_outbound_url", side_effect=BOOM),
    ):
        client.get(
            "/api/settings/openai-compat/models",
            params={"baseUrl": f"http://evil\n{forged}"},
        )

    summary = next(
        line for line in caplog.text.splitlines() if "OpenAI-compatible" in line
    )
    assert forged in summary, "the payload must stay readable, escaped not stripped"
    assert "\\n" in summary, "an unescaped newline forges a second log record"


# --------------------------------------------------------------------------
# The file-backed handlers (#1301 follow-up)
#
# The first pass on #1301 worked from the CodeQL alert list and closed every
# flagged line. Five `raise HTTPException(detail=f"...{str(e)}")` handlers
# survived it, because the engine never connected those sinks -- which is the
# trap the issue itself called out: "the flagged line count understates the
# bug. Read the handlers, do not work only from the alert list."
#
# These three routes write to disk, so the exception they catch is an OSError
# that renders the ABSOLUTE PATH it failed on. `.env` is one of those paths.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "post",
            "/api/settings/api-key",
            # sk-ant- prefix: the handler validates the key FORMAT first and
            # returns 400 without ever reaching the disk write, so a placeholder
            # key makes this test pass without exercising anything.
            {
                "keyType": "anthropic",
                "keyValue": "sk-ant-" + "x" * 40,
                "saveToEnv": True,
            },
        ),
        ("put", "/api/settings/tab-state", {"tabs": ["a"]}),
        ("patch", "/api/settings/source-env", {"DEBUG": "true"}),
    ],
)
def test_a_disk_failure_does_not_put_our_paths_in_the_response(
    client: TestClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    """An OSError names the path it failed on; the caller must not receive it.

    ``BOOM`` is a ``ConnectionRefusedError``, which subclasses ``OSError``, so
    it is caught by the same ``except OSError`` these handlers use while still
    carrying a path, a host and a port to assert the absence of.
    """
    with (
        patch("pathlib.Path.write_text", side_effect=BOOM),
        patch("pathlib.Path.read_text", side_effect=BOOM),
    ):
        response = client.request(method.upper(), path, json=body)

    # Pinned: a 4xx here means validation rejected the request before the disk
    # write, and _assert_no_leak would pass without exercising the handler.
    assert response.status_code == 500, (
        f"{path} returned {response.status_code}; the handler was never reached, "
        f"so this assertion proves nothing: {response.text!r}"
    )
    _assert_no_leak(response.text)


def test_an_unserialisable_tab_state_does_not_echo_the_exception(
    client: TestClient,
) -> None:
    """The 400 branch leaked too, and a 4xx body is still a response body.

    ``save_tab_state`` caught ``(TypeError, ValueError)`` from ``json.dumps``
    and echoed ``str(e)``. Lower severity than the OSError branch, but the same
    shape, and it is the branch a caller can reach on demand.
    """

    class FakeJson:
        """Swapped into settings.py's namespace only.

        Patching ``json.dumps`` globally also breaks the response serialisation
        starlette does on the way out, which turns this 400 into a 500 and the
        assertion stops testing the branch it names.
        """

        JSONDecodeError = json.JSONDecodeError
        loads = staticmethod(json.loads)

        @staticmethod
        def dumps(*_args: object, **_kwargs: object) -> str:
            raise TypeError(f"not serialisable while writing {CREDENTIALS_FILE}")

    with patch.object(settings_routes, "json", FakeJson):
        response = client.put("/api/settings/tab-state", json={"tabs": ["a"]})

    assert response.status_code == 400, "must reach the 400 branch, not fail earlier"
    _assert_no_leak(response.text)
