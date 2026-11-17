"""Tests for the in-house model gateway (Factory#1959).

Covers: /healthz, 503 when unconfigured, 401 on a bad bearer, 413 on an
oversize body, forwarding (upstream key swapped in, status and body passed
back), 502 when the upstream is down, and ``load_config`` rejecting a
non-http upstream URL.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "apps" / "model-gateway"))

import gateway  # noqa: E402

TOKEN = "client-secret"


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def upstream():
    seen: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.update(
                path=self.path, auth=self.headers.get("Authorization"), body=body
            )
            self.send_response(seen.get("status", 201))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    yield _serve(server), seen
    server.shutdown()
    server.server_close()


def _gateway(env):
    server = gateway.make_server(gateway.load_config(env), "127.0.0.1", 0)
    return server, _serve(server)


def _post(base, body=b"{}", token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(
        base + "/v1/chat/completions", body, headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers.get("X-Model-Route")
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers.get("X-Model-Route")


def test_healthz():
    server, base = _gateway({})
    with urllib.request.urlopen(base + "/healthz", timeout=5) as resp:
        assert resp.status == 200
        assert json.loads(resp.read()) == {"status": "ok"}
    server.shutdown()


def test_unconfigured_returns_503():
    server, base = _gateway({})
    assert _post(base)[0] == 503
    server.shutdown()


def test_wrong_token_returns_401(upstream):
    url, _ = upstream
    server, base = _gateway(
        {"GATEWAY_UPSTREAM_URL": url, "GATEWAY_CLIENT_TOKEN": TOKEN}
    )
    assert _post(base, token="nope")[0] == 401
    assert _post(base, token=None)[0] == 401
    server.shutdown()


def test_oversize_returns_413(upstream):
    url, _ = upstream
    server, base = _gateway(
        {"GATEWAY_UPSTREAM_URL": url, "GATEWAY_CLIENT_TOKEN": TOKEN}
    )
    # Declare an oversize length without sending it; the gateway must reject on the header.
    conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
    conn.putrequest("POST", "/v1/chat/completions")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", str(10 * 1024 * 1024 + 1))
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()
    server.shutdown()


def test_forward_swaps_auth_and_passes_response_back(upstream):
    url, seen = upstream
    env = {
        "GATEWAY_UPSTREAM_URL": url,
        "GATEWAY_UPSTREAM_API_KEY": "upstream-key",
        "GATEWAY_CLIENT_TOKEN": TOKEN,
    }
    server, base = _gateway(env)
    assert _post(base, body=b'{"m":1}') == (201, b'{"ok":true}', None)
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer upstream-key"
    assert seen["body"] == b'{"m":1}'
    server.shutdown()


def test_upstream_down_returns_502(tmp_path):
    dead = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    url = f"http://127.0.0.1:{dead.server_address[1]}"
    dead.server_close()
    routes = tmp_path / "routes.json"
    routes.write_text('{"cheap":"routed"}')
    server, base = _gateway(
        {
            "GATEWAY_UPSTREAM_URL": url,
            "GATEWAY_CLIENT_TOKEN": TOKEN,
            "GATEWAY_ROUTES_PATH": str(routes),
        }
    )
    status, _, route = _post(base, b'{"model":"cheap"}')
    assert (status, route) == (502, None)
    server.shutdown()


def test_routes_resolve_and_reload(upstream, tmp_path):
    url, seen = upstream
    routes = tmp_path / "routes.json"
    routes.write_text('{"cheap":"first-model"}')
    server, base = _gateway(
        {
            "GATEWAY_UPSTREAM_URL": url,
            "GATEWAY_CLIENT_TOKEN": TOKEN,
            "GATEWAY_ROUTES_PATH": str(routes),
        }
    )
    assert _post(base, b'{"model":"cheap","messages":[]}') == (
        201,
        b'{"ok":true}',
        "cheap",
    )
    assert json.loads(seen["body"])["model"] == "first-model"
    routes.write_text('{"cheap":"second-model"}')
    seen["status"] = 429
    assert _post(base, b'{"model":"cheap"}') == (429, b'{"ok":true}', "cheap")
    assert json.loads(seen["body"])["model"] == "second-model"
    server.shutdown()


@pytest.mark.parametrize(
    ("routes_content", "body"),
    [
        ('{"cheap":"routed"}', b'{ "model": "concrete" }'),
        ('{"concrete":"routed"}', b'{ "model": "concrete" }'),
        ('{"cheap":"routed"}', b'{ "model": "local" }'),
        ('{"local":""}', b'{ "model": "local" }'),
        ('{"local":7}', b'{ "model": "local" }'),
        ('{"cheap":"routed"}', b"not json"),
        ('{"cheap":"routed"}', b'["cheap"]'),
        ("not json", b'{ "model": "cheap" }'),
    ],
)
def test_unresolved_requests_are_unchanged(upstream, tmp_path, routes_content, body):
    url, seen = upstream
    routes = tmp_path / "routes.json"
    routes.write_text(routes_content)
    server, base = _gateway(
        {
            "GATEWAY_UPSTREAM_URL": url,
            "GATEWAY_CLIENT_TOKEN": TOKEN,
            "GATEWAY_ROUTES_PATH": str(routes),
        }
    )
    assert _post(base, body) == (201, b'{"ok":true}', None)
    assert seen["body"] == body
    server.shutdown()


@pytest.mark.parametrize("routes_path", [None, "missing"])
def test_no_routes_file_forwards_unchanged(upstream, tmp_path, routes_path):
    url, seen = upstream
    env = {"GATEWAY_UPSTREAM_URL": url, "GATEWAY_CLIENT_TOKEN": TOKEN}
    if routes_path:
        env["GATEWAY_ROUTES_PATH"] = str(tmp_path / routes_path)
    server, base = _gateway(env)
    body = b'{ "model": "cheap" }'
    assert _post(base, body) == (201, b'{"ok":true}', None)
    assert seen["body"] == body
    server.shutdown()


def test_load_config_rejects_non_http_upstream():
    with pytest.raises(ValueError):
        gateway.load_config({"GATEWAY_UPSTREAM_URL": "file:///x"})


def test_redirect_is_not_followed(upstream):
    target_url, target_seen = upstream

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302)
            self.send_header("Location", target_url + "/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    env = {
        "GATEWAY_UPSTREAM_URL": _serve(redirector),
        "GATEWAY_UPSTREAM_API_KEY": "upstream-key",
        "GATEWAY_CLIENT_TOKEN": TOKEN,
    }
    server, base = _gateway(env)
    assert _post(base)[0] == 302
    assert target_seen == {}
    server.shutdown()
    redirector.shutdown()
    redirector.server_close()


def test_silent_client_is_disconnected(monkeypatch):
    monkeypatch.setattr(gateway, "CLIENT_TIMEOUT", 1)
    server, _ = _gateway({})
    sock = socket.create_connection(server.server_address, timeout=5)
    try:
        assert sock.recv(1) == b""  # gateway closed the idle socket
    finally:
        sock.close()
        server.shutdown()
        server.server_close()
