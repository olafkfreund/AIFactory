"""Loopback tests for the gateway's best-effort OTLP exporter."""

from __future__ import annotations

import json
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "apps" / "model-gateway"))

import gateway  # noqa: E402


@pytest.fixture
def collector():
    servers = []

    def make(redirect=""):
        seen = []
        arrived = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.append((self.path, dict(self.headers), json.loads(body)))
                arrived.set()
                self.send_response(302 if redirect else 200)
                if redirect:
                    self.send_header("Location", redirect)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}", seen, arrived

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("ok,code", [(True, 1), (False, 2)])
def test_exports_typed_span(collector, ok, code):
    url, seen, arrived = collector()
    telemetry = gateway._Telemetry(url + "/", " X-Test = first=second ")
    telemetry.emit(
        {
            "factory.genai.route": "cheap",
            "gen_ai.usage.input_tokens": 7,
            "factory.genai.cost_usd": 0.25,
            "factory.genai.cached": True,
        },
        123,
        456,
        ok,
    )
    assert arrived.wait(5)
    path, headers, payload = seen[0]
    assert path == "/v1/traces"
    assert headers["X-Test"] == "first=second"
    assert headers["Content-Type"] == "application/json"
    resource = payload["resourceSpans"][0]
    assert resource["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "model-gateway"}}
    ]
    scope = resource["scopeSpans"][0]
    assert scope["scope"] == {"name": "model-gateway"}
    span = scope["spans"][0]
    assert len(span["traceId"]) == 32
    assert len(span["spanId"]) == 16
    assert span["name"] == "chat cheap"
    assert span["kind"] == 3
    assert (span["startTimeUnixNano"], span["endTimeUnixNano"]) == ("123", "456")
    assert span["status"] == {"code": code}
    assert {item["key"]: item["value"] for item in span["attributes"]} == {
        "factory.genai.route": {"stringValue": "cheap"},
        "gen_ai.usage.input_tokens": {"intValue": "7"},
        "factory.genai.cost_usd": {"doubleValue": 0.25},
        "factory.genai.cached": {"boolValue": True},
    }


def test_disabled_does_not_start_worker():
    telemetry = gateway._Telemetry("")
    assert not telemetry.enabled
    assert not hasattr(telemetry, "thread")
    telemetry.emit({"factory.genai.route": "cheap"}, 1, 2, True)


def test_redirect_is_not_followed(collector):
    target, target_seen, target_arrived = collector()
    source, source_seen, source_arrived = collector(target + "/v1/traces")
    gateway._Telemetry(source, "Authorization=secret").emit({}, 1, 2, True)
    assert source_arrived.wait(5)
    assert source_seen[0][0] == "/v1/traces"
    assert not target_arrived.wait(0.2)
    assert not target_seen


def test_dead_collector_does_not_block_emit():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    telemetry = gateway._Telemetry(f"http://127.0.0.1:{port}")
    start = time.monotonic()
    telemetry.emit({}, 1, 2, False)
    assert time.monotonic() - start < 1


def test_full_queue_drops_without_raising(monkeypatch):
    telemetry = gateway._Telemetry("http://127.0.0.1:1")

    def full(*args):
        raise queue.Full

    monkeypatch.setattr(telemetry.queue, "put_nowait", full)
    telemetry.emit({}, 1, 2, True)
