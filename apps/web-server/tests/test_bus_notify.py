"""Matrix notice configuration and failure handling."""

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from server.services.bus_notify import notify_question_waiting, portal_base_url

HOMESERVER = "https://matrix.example"
ROOM = "!room:matrix.example"
PORTAL = "https://portal.example"
TOKEN = uuid4().hex
BODY = (
    "[factory] a question is waiting for you — "
    "run aifactory.questions_pending or open https://portal.example/needs-input"
)
ENV = {
    "AIFACTORY_BUS_HOMESERVER": HOMESERVER,
    "AIFACTORY_BUS_ROOM": ROOM,
    "AIFACTORY_PORTAL_URL": f"{PORTAL}/",
    "AIFACTORY_BUS_TOKEN_FILE": "",
}


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bot-token"
    path.write_text(f"{TOKEN}\n")
    for key, value in {**ENV, "AIFACTORY_BUS_TOKEN_FILE": str(path)}.items():
        monkeypatch.setenv(key, value)
    return path


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"event_id": "$example"})

    def put(url: str, **kwargs: Any) -> httpx.Response:
        assert kwargs.pop("follow_redirects") is False
        assert kwargs.pop("trust_env") is False
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            return client.put(url, **kwargs)

    monkeypatch.setattr(httpx, "put", put)
    return captured


@pytest.mark.usefixtures("token_file")
def test_notice_is_fixed_and_content_free(requests: list[httpx.Request]) -> None:
    assert portal_base_url() == PORTAL
    notify_question_waiting()
    notify_question_waiting()
    assert len(requests) == 2
    first, second = requests
    assert first.method == "PUT"
    assert first.url.scheme == "https" and first.url.host == "matrix.example"
    assert first.url.raw_path.startswith(
        b"/_matrix/client/v3/rooms/%21room%3Amatrix.example/send/m.room.message/"
    )
    assert first.url != second.url
    assert not first.url.query
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(first.content) == {"msgtype": "m.text", "body": BODY}
    for private in ("Which timezone?", "Secret project", "a:001-build", "UTC"):
        assert private not in BODY


@pytest.mark.parametrize("key", ENV)
@pytest.mark.usefixtures("token_file")
def test_no_request_when_setting_missing(
    requests: list[httpx.Request], monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.delenv(key)
    notify_question_waiting()
    assert requests == []


@pytest.mark.parametrize("exists", [False, True])
def test_no_request_when_token_missing_or_empty(
    requests: list[httpx.Request],
    token_file: Path,
    exists: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if exists:
        token_file.write_text(" \n")
    else:
        token_file.unlink()
    notify_question_waiting()
    assert requests == []
    assert not caplog.records


@pytest.mark.parametrize(
    "url",
    [
        "http://matrix.example",
        "https://user:private@matrix.example",
        "https://[bad",
        "https://matrix.example:bad",
    ],
)
@pytest.mark.usefixtures("token_file")
def test_invalid_homeserver_does_not_send_or_log_credentials(
    requests: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    url: str,
) -> None:
    monkeypatch.setenv("AIFACTORY_BUS_HOMESERVER", url)
    notify_question_waiting()
    assert requests == []
    assert len(caplog.records) == 1
    assert "private" not in caplog.text and TOKEN not in caplog.text


@pytest.mark.usefixtures("token_file")
def test_redirect_is_never_followed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.example/"})

    def put(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            kwargs.pop("trust_env")
            return client.put(url, **kwargs)

    monkeypatch.setattr(httpx, "put", put)
    notify_question_waiting()
    assert [request.url.host for request in captured] == ["matrix.example"]
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "failure", ["timeout", "dns", "5xx", "bad-json", "exception", "file"]
)
def test_failure_returns_and_logs_only_exception_class(
    token_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    def put(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("PUT", url)
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        if failure == "dns":
            raise httpx.ConnectError(TOKEN, request=request)
        if failure == "exception":
            raise RuntimeError(TOKEN)
        if failure == "5xx":
            return httpx.Response(503, text=TOKEN, request=request)
        return httpx.Response(200, text=TOKEN, request=request)

    monkeypatch.setattr(httpx, "put", put)
    if failure == "file":
        monkeypatch.setenv("AIFACTORY_BUS_TOKEN_FILE", str(token_file.parent))
    notify_question_waiting()
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("Question bus notice failed (")
    assert record.exc_info is None
    assert TOKEN not in caplog.text
