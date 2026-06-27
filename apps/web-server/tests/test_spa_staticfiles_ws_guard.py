"""SPAStaticFiles must not assert-crash on non-HTTP (websocket) scopes.

The SPA is mounted at the catch-all ``/``. A websocket connection to a path with
no matching WS route falls through the router to this mount. Starlette's
``StaticFiles.__call__`` opens with ``assert scope["type"] == "http"``, which
raised ``AssertionError`` for every stray websocket — spamming the logs and
breaking the handshake with a 500. The override rejects non-HTTP scopes cleanly.
"""

import pytest
from server.main import SPAStaticFiles


def _static(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    return SPAStaticFiles(directory=str(tmp_path), html=True)


@pytest.mark.asyncio
async def test_websocket_scope_is_closed_not_asserted(tmp_path):
    static = _static(tmp_path)
    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        sent.append(msg)

    # Must NOT raise AssertionError; must reject the handshake with a close
    # (a close sent before accept is the ASGI way to deny a websocket).
    await static({"type": "websocket", "path": "/no/such/ws"}, receive, send)

    assert sent == [{"type": "websocket.close", "code": 1000}]


@pytest.mark.asyncio
async def test_non_http_non_ws_scope_returns_cleanly(tmp_path):
    static = _static(tmp_path)
    sent: list[dict] = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(msg):
        sent.append(msg)

    # A lifespan (or any other non-http) scope must return without asserting
    # and without emitting messages it has no business sending.
    await static({"type": "lifespan"}, receive, send)

    assert sent == []


@pytest.mark.asyncio
async def test_http_scope_still_served(tmp_path):
    static = _static(tmp_path)
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        messages.append(msg)

    # An ordinary HTTP GET for index.html is served as before (delegates to
    # StaticFiles.__call__ -> get_response), proving the guard only short-circuits
    # non-HTTP scopes.
    await static(
        {
            "type": "http",
            "method": "GET",
            "path": "/index.html",
            "headers": [],
        },
        receive,
        send,
    )

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 200
