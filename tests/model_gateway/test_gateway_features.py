"""Quota, accounting, failover and metrics of the model gateway (Factory#1959).

Fake upstreams on loopback only: no provider keys, no network.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "apps" / "model-gateway"))

import gateway  # noqa: E402

TOKEN = "client-secret"
USAGE = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}


class Fake:
    """A fake upstream: status/body/delay are mutable, requests are recorded."""

    def __init__(self, status=200, body=None, delay=0.0):
        self.status, self.delay = status, delay
        self.body = json.dumps(body if body is not None else USAGE).encode()
        self.seen: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append(
                    {"auth": self.headers.get("Authorization"), "body": data}
                )
                time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    self.wfile.write(outer.body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fakes():
    made = []

    def make(**kw):
        made.append(Fake(**kw))
        return made[-1]

    yield make
    for fake in made:
        fake.close()


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(gateway, "_now", lambda: now[0])
    return now


def _start(env, tmp_path, routes=None):
    env = {"GATEWAY_CLIENT_TOKEN": TOKEN, **env}
    if routes is not None:
        path = tmp_path / "routes.json"
        path.write_text(json.dumps(routes) if not isinstance(routes, str) else routes)
        env["GATEWAY_ROUTES_PATH"] = str(path)
    server = gateway.make_server(gateway.load_config(env), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _call(base, path="/v1/chat/completions", body=None, headers=None, token=TOKEN):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = (
        None if body is None and path == "/metrics" else json.dumps(body or {}).encode()
    )
    req = urllib.request.Request(
        base + path, data, h, method="GET" if data is None else "POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers


def _metric(text, name, **labels):
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name + "{") and all(
            f'{k}="{v}"' in line for k, v in labels.items()
        ):
            total += float(line.rpartition(" ")[2])
    return total


def _scrape(base):
    return _call(base, "/metrics")[1].decode()


# --- quota -----------------------------------------------------------------


def test_request_quota_429_retry_after_and_window_reset(fakes, tmp_path, clock):
    up = fakes()
    routes = {"cheap": {"model": "m", "quota": {"window_s": 60, "requests": 2}}}
    server, base = _start({"GATEWAY_UPSTREAM_URL": up.url}, tmp_path, routes)
    body = {"model": "cheap"}
    assert _call(base, body=body)[0] == 200
    clock[0] += 10
    assert _call(base, body=body)[0] == 200
    status, data, headers = _call(base, body=body)
    assert status == 429
    assert json.loads(data) == {"error": "route quota exceeded"}
    assert headers["Retry-After"] == "50"
    assert headers["X-Model-Route"] == "cheap"
    assert len(up.seen) == 2
    clock[0] += 51
    assert _call(base, body=body)[0] == 200
    server.shutdown()


def test_token_quota_charges_completed_usage(fakes, tmp_path, clock):
    up = fakes()  # 150 tokens per response
    routes = {"cheap": {"model": "m", "quota": {"tokens": 200}}}
    server, base = _start({"GATEWAY_UPSTREAM_URL": up.url}, tmp_path, routes)
    body = {"model": "cheap"}
    assert [_call(base, body=body)[0] for _ in range(3)] == [200, 200, 429]
    server.shutdown()


def test_concrete_model_is_exempt_from_quota(fakes, tmp_path, clock):
    up = fakes()
    routes = {"cheap": {"model": "m", "quota": {"requests": 1}}}
    server, base = _start({"GATEWAY_UPSTREAM_URL": up.url}, tmp_path, routes)
    assert [_call(base, body={"model": "concrete"})[0] for _ in range(3)] == [200] * 3
    server.shutdown()


def test_quota_hot_reload_and_invalid_reload_keeps_last_valid(fakes, tmp_path, clock):
    up = fakes()
    routes = {"cheap": {"model": "m", "quota": {"requests": 1}}}
    server, base = _start({"GATEWAY_UPSTREAM_URL": up.url}, tmp_path, routes)
    path = tmp_path / "routes.json"
    body = {"model": "cheap"}
    assert _call(base, body=body)[0] == 200
    assert _call(base, body=body)[0] == 429
    path.write_text("not json")  # bad reload: the quota must stay
    assert _call(base, body=body)[0] == 429
    path.write_text(json.dumps({"cheap": {"model": "m", "quota": {"requests": -1}}}))
    assert _call(base, body=body)[0] == 429
    path.write_text(json.dumps({"cheap": {"model": "m", "quota": {"requests": 5}}}))
    assert _call(base, body=body)[0] == 200  # hot reload took effect
    server.shutdown()


def test_window_change_starts_new_window(tmp_path, clock):
    state = gateway._State(gateway.load_config({}))
    assert state.admit("cheap", {"window_s": 60, "requests": 1}) == 0
    assert state.admit("cheap", {"window_s": 60, "requests": 1}) == 60
    assert state.admit("cheap", {"window_s": 30, "requests": 1}) == 0


def test_concurrent_admission_never_exceeds_limit(clock):
    state = gateway._State(gateway.load_config({}))
    quota = {"window_s": 60, "requests": 10}
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(state.admit("cheap", quota)))
        for _ in range(60)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(0) == 10


# --- accounting and metrics ---------------------------------------------------


def _catalog(tmp_path, models):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"models": models}))
    return str(path)


def test_metrics_off_by_default(fakes, tmp_path):
    up = fakes()
    server, base = _start({"GATEWAY_UPSTREAM_URL": up.url}, tmp_path)
    assert _call(base, "/metrics")[0] == 404
    assert _call(base, body={"model": "x"})[0] == 200
    server.shutdown()


def test_metrics_auth(fakes, tmp_path):
    up = fakes()
    server, base = _start(
        {"GATEWAY_UPSTREAM_URL": up.url, "GATEWAY_METRICS": "on"}, tmp_path
    )
    assert _call(base, "/metrics", token=None)[0] == 401
    assert _call(base, "/metrics")[0] == 200
    server.shutdown()


def test_usage_cost_and_labels_json(fakes, tmp_path):
    up = fakes(
        body={
            "model": "m",
            "usage": {"prompt_tokens": 1000000, "completion_tokens": 500000},
        }
    )
    cat = _catalog(
        tmp_path,
        {"m": {"price": {"mode": "metered", "in_per_mtok": 2, "out_per_mtok": 4}}},
    )
    env = {
        "GATEWAY_UPSTREAM_URL": up.url,
        "GATEWAY_METRICS": "on",
        "GATEWAY_CATALOG_PATH": cat,
    }
    server, base = _start(env, tmp_path, {"cheap": "m"})
    assert (
        _call(base, body={"model": "cheap"}, headers={"X-Caller-Service": "tfactory"})[
            0
        ]
        == 200
    )
    text = _scrape(base)
    labels = {"route": "cheap", "service": "tfactory"}
    assert _metric(text, "gateway_requests_total", **labels, outcome="ok") == 1
    assert _metric(text, "gateway_tokens_total", **labels, kind="prompt") == 1000000
    assert _metric(text, "gateway_tokens_total", **labels, kind="completion") == 500000
    assert _metric(text, "gateway_cost_usd_total", **labels) == pytest.approx(4.0)
    assert _metric(text, "gateway_unpriced_total") == 0
    server.shutdown()


def test_sse_usage_stream_options_and_unpriced(fakes, tmp_path):
    sse = 'data: {"model":"free","choices":[]}\n\ndata: {"model":"free","usage":{"prompt_tokens":7,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
    up = fakes()
    up.body = sse.encode()
    env = {
        "GATEWAY_UPSTREAM_URL": up.url,
        "GATEWAY_METRICS": "on",
        "GATEWAY_CATALOG_PATH": str(tmp_path / "none.json"),
    }
    server, base = _start(env, tmp_path)
    assert _call(base, body={"model": "free", "stream": True})[0] == 200
    assert json.loads(up.seen[0]["body"])["stream_options"] == {"include_usage": True}
    text = _scrape(base)
    assert (
        _metric(
            text,
            "gateway_tokens_total",
            route="direct",
            service="unknown",
            kind="prompt",
        )
        == 7
    )
    assert _metric(text, "gateway_tokens_total", kind="completion") == 3
    assert _metric(text, "gateway_unpriced_total", route="direct") == 1
    server.shutdown()


def test_no_stream_options_when_metrics_off(fakes, tmp_path):
    up = fakes()
    server, base = _start({"GATEWAY_UPSTREAM_URL": up.url}, tmp_path)
    body = b'{"model":"x","stream":true}'
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        body,
        {"Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()
    assert up.seen[0]["body"] == body
    server.shutdown()


def test_missing_usage_is_counted(fakes, tmp_path):
    up = fakes(body={"ok": True})
    server, base = _start(
        {"GATEWAY_UPSTREAM_URL": up.url, "GATEWAY_METRICS": "on"}, tmp_path
    )
    _call(base, body={"model": "x"})
    text = _scrape(base)
    assert _metric(text, "gateway_usage_missing_total", route="direct") == 1
    assert _metric(text, "gateway_tokens_total") == 0
    server.shutdown()


def test_service_label_sanitised_and_capped(fakes, tmp_path):
    up = fakes()
    server, base = _start(
        {"GATEWAY_UPSTREAM_URL": up.url, "GATEWAY_METRICS": "on"}, tmp_path
    )
    _call(base, body={"model": "x"}, headers={"X-Caller-Service": 'bad"svc\\'})
    for i in range(gateway.MAX_SERVICES + 2):
        _call(base, body={"model": "x"}, headers={"X-Caller-Service": f"svc-{i}"})
    text = _scrape(base)
    assert _metric(text, "gateway_requests_total", service="unknown") == 1
    assert _metric(text, "gateway_requests_total", service="other") == 2
    assert 'svc-0"' in text and "bad" not in text
    server.shutdown()


def test_error_outcomes_counted(fakes, tmp_path):
    up = fakes(status=400, body={"error": "bad"})
    server, base = _start(
        {"GATEWAY_UPSTREAM_URL": up.url, "GATEWAY_METRICS": "on"}, tmp_path
    )
    assert _call(base, body={"model": "x"})[0] == 400
    assert _metric(_scrape(base), "gateway_requests_total", outcome="client_error") == 1
    server.shutdown()


def test_self_test_passes(capsys):
    gateway._self_test()
    assert "self-test ok" in capsys.readouterr().out


# --- failover ----------------------------------------------------------------


def _two(fakes, tmp_path, a, b, extra_env=None):
    routes = {
        "cheap": {
            "model": "m",
            "upstreams": [
                {"url": a.url, "key_env": "KEY_A"},
                {"url": b.url, "key_env": "KEY_B", "model": "m-b"},
            ],
        }
    }
    env = {
        "GATEWAY_UPSTREAM_URL": a.url,
        "GATEWAY_METRICS": "on",
        "GATEWAY_COOLDOWN_S": "30",
        **(extra_env or {}),
    }
    return _start(env, tmp_path, routes)


def test_failover_then_recover_both_directions(fakes, tmp_path, clock, monkeypatch):
    monkeypatch.setenv("KEY_A", "key-a")
    monkeypatch.setenv("KEY_B", "key-b")
    a, b = fakes(), fakes()
    server, base = _two(fakes, tmp_path, a, b)
    body = {"model": "cheap"}
    assert _call(base, body=body)[0] == 200 and (len(a.seen), len(b.seen)) == (1, 0)
    a.status = 503  # primary fails: served by the secondary
    status, _, headers = _call(base, body=body)
    assert status == 200 and len(b.seen) == 1
    assert headers["X-Model-Failover-From"].startswith("127.0.0.1")
    assert json.loads(b.seen[0]["body"])["model"] == "m-b"
    _call(base, body=body)  # A is cooling down: goes straight to B
    assert (len(a.seen), len(b.seen)) == (2, 2)
    clock[0] += 31
    a.status = 200  # cooldown over and A healthy again: the probe returns to A
    assert _call(base, body=body)[0] == 200
    assert (len(a.seen), len(b.seen)) == (3, 2)
    b.status, a.status = 503, 200  # reverse direction: B down now, A serves
    assert _call(base, body=body)[0] == 200 and len(a.seen) == 4
    a.status = 503  # B (cooling) is the fallback of last resort
    b.status = 200
    clock[0] += 31
    assert _call(base, body=body)[0] == 200
    text = _scrape(base)
    assert _metric(text, "gateway_failovers_total", route="cheap") >= 2
    assert _metric(text, "gateway_requests_total", outcome="ok") >= 5
    server.shutdown()


def test_no_failover_on_4xx(fakes, tmp_path, clock):
    a, b = fakes(status=429, body={"error": "rate"}), fakes()
    server, base = _two(fakes, tmp_path, a, b)
    assert _call(base, body={"model": "cheap"})[0] == 429
    assert len(b.seen) == 0
    server.shutdown()


def test_failover_on_connection_error(fakes, tmp_path, clock):
    a, b = fakes(), fakes()
    a.close()
    server, base = _two(fakes, tmp_path, a, b)
    assert _call(base, body={"model": "cheap"})[0] == 200
    assert len(b.seen) == 1
    server.shutdown()


def test_failover_on_timeout(fakes, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(gateway, "UPSTREAM_TIMEOUT", 0.2)
    a, b = fakes(delay=1.0), fakes()
    server, base = _two(fakes, tmp_path, a, b)
    assert _call(base, body={"model": "cheap"})[0] == 200
    assert len(b.seen) == 1
    server.shutdown()


def test_all_upstreams_down_returns_last_failure(fakes, tmp_path, clock):
    a, b = fakes(status=500, body={"e": 1}), fakes(status=502, body={"e": 2})
    server, base = _two(fakes, tmp_path, a, b)
    status, data, _ = _call(base, body={"model": "cheap"})
    assert (status, json.loads(data)) == (502, {"e": 2})
    server.shutdown()


def test_keys_do_not_cross_hosts(fakes, tmp_path, clock, monkeypatch):
    monkeypatch.setenv("KEY_A", "key-a")
    monkeypatch.delenv("KEY_B", raising=False)
    a, b = fakes(status=503), fakes()
    server, base = _two(
        fakes, tmp_path, a, b, {"GATEWAY_UPSTREAM_API_KEY": "global-key"}
    )
    _call(base, body={"model": "cheap"})
    assert a.seen[0]["auth"] == "Bearer key-a"
    assert b.seen[0]["auth"] is None  # neither key-a nor the global key
    server.shutdown()


def test_default_upstream_keeps_global_key_for_direct(fakes, tmp_path):
    up = fakes()
    env = {"GATEWAY_UPSTREAM_URL": up.url, "GATEWAY_UPSTREAM_API_KEY": "global-key"}
    server, base = _start(env, tmp_path)
    _call(base, body={"model": "x"})
    assert up.seen[0]["auth"] == "Bearer global-key"
    server.shutdown()


def test_upstream_gauge_uses_host_only_and_tracks_health(fakes, tmp_path, clock):
    a, b = fakes(status=503), fakes()
    server, base = _two(fakes, tmp_path, a, b)
    _call(base, body={"model": "cheap"})
    text = _scrape(base)
    host_a = a.url.removeprefix("http://")
    assert f'gateway_upstream_up{{upstream="{host_a}"}} 0' in text
    assert "http://" not in text
    server.shutdown()


def test_request_emits_populated_span(fakes, tmp_path):
    seen: list = []
    arrived = threading.Event()

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            arrived.set()
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    collector = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=collector.serve_forever, daemon=True).start()
    up = fakes(
        body={"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    )
    cat = _catalog(
        tmp_path,
        {"m": {"price": {"mode": "metered", "in_per_mtok": 1, "out_per_mtok": 1}}},
    )
    env = {
        "GATEWAY_UPSTREAM_URL": up.url,
        "GATEWAY_CATALOG_PATH": cat,
        "GATEWAY_OTLP_ENDPOINT": f"http://127.0.0.1:{collector.server_address[1]}",
    }
    server, base = _start(env, tmp_path, {"cheap": "m"})
    assert (
        _call(
            base,
            body={
                "model": "cheap",
                "messages": [{"role": "user", "content": "SECRET-PROMPT"}],
            },
            headers={"X-Caller-Service": "cfactory"},
        )[0]
        == 200
    )
    assert arrived.wait(5)
    span = seen[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}
    assert attrs["factory.genai.route"] == {"stringValue": "cheap"}
    assert attrs["factory.genai.caller_service"] == {"stringValue": "cfactory"}
    assert attrs["factory.genai.resolved_model"] == {"stringValue": "m"}
    assert attrs["factory.genai.outcome"] == {"stringValue": "ok"}
    assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "10"}
    assert attrs["gen_ai.usage.output_tokens"] == {"intValue": "5"}
    assert attrs["gen_ai.request.model"] == {"stringValue": "cheap"}
    assert "factory.genai.latency_ms" in attrs and "factory.genai.cost_usd" in attrs
    assert "SECRET-PROMPT" not in json.dumps(seen)
    server.shutdown()
    collector.shutdown()


def test_stale_charge_does_not_reset_new_window(clock):
    state = gateway._State(gateway.load_config({}))
    old, new = {"window_s": 60, "tokens": 10}, {"window_s": 30, "tokens": 10}
    state.charge("cheap", new, 10)
    assert state.admit("cheap", new) > 0
    state.charge("cheap", old, 5)  # response admitted under the old config
    assert state.admit("cheap", new) > 0


def _raw_upstream(status, partial_body, stall=0.0):
    """Upstream that promises more body than it sends."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)

    def run():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.recv(65536)
            conn.sendall(
                f"HTTP/1.1 {status} X\r\nContent-Length: 100\r\n\r\n".encode()
                + partial_body
            )
            time.sleep(stall)
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return sock, f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_truncated_5xx_body_still_fails_over(fakes, tmp_path, clock):
    sock, bad = _raw_upstream(503, b"par")
    b = fakes()
    a = type("A", (), {"url": bad})
    server, base = _two(fakes, tmp_path, a, b)
    assert _call(base, body={"model": "cheap"})[0] == 200
    assert len(b.seen) == 1
    server.shutdown()
    sock.close()


def test_stalled_4xx_body_does_not_fail_over(fakes, tmp_path, clock, monkeypatch):
    monkeypatch.setattr(gateway, "UPSTREAM_TIMEOUT", 0.3)
    sock, bad = _raw_upstream(429, b"par", stall=1.0)
    b = fakes()
    a = type("A", (), {"url": bad})
    server, base = _two(fakes, tmp_path, a, b)
    assert _call(base, body={"model": "cheap"})[0] == 429
    assert len(b.seen) == 0
    server.shutdown()
    sock.close()
