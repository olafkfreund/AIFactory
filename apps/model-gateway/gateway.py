"""Minimal in-house model gateway (Factory#1959). Stdlib only.

Forwards ``POST /v1/chat/completions`` to an OpenAI-compatible upstream,
swapping the caller's bearer token for the upstream API key.

Env: GATEWAY_UPSTREAM_URL, GATEWAY_UPSTREAM_API_KEY (optional),
GATEWAY_CLIENT_TOKEN, GATEWAY_ROUTES_PATH (optional), GATEWAY_PORT (default 8080).

Optional, all off by default (Factory#1959):
  GATEWAY_METRICS=on       token/cost accounting and ``GET /metrics``
  GATEWAY_CATALOG_PATH     price catalog (default ``model-catalog.json`` beside this file)
  GATEWAY_COOLDOWN_S       failover cooldown in seconds (default 30)
  GATEWAY_OTLP_ENDPOINT    OTLP/HTTP base URL; spans are exported only when set
  GATEWAY_OTLP_HEADERS     ``k=v,k2=v2`` request headers for the exporter

A routes-file value may be a model string (as before) or an object
``{"model": id, "quota": {"window_s", "requests", "tokens"},
"upstreams": [{"url", "key_env", "model"}]}``. ``key_env`` names an env var.
``python gateway.py --self-test`` checks the accounting end to end on loopback.
"""

from __future__ import annotations

import hmac
import http.client
import json
import math
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MAX_BODY = 10 * 1024 * 1024
UPSTREAM_TIMEOUT = 120
# Per-socket inbound timeout: without it a silent or short-body client holds a
# handler thread forever (slowloris, and rfile.read on a short body).
CLIENT_TIMEOUT = 30
ROUTE_CLASSES = {"local", "cheap", "balanced", "frontier"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A followed redirect would carry the upstream key to another host.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


@dataclass(frozen=True)
class Config:
    upstream_url: str
    upstream_key: str
    client_token: str
    routes_path: str
    metrics: bool = False
    catalog_path: str = ""
    cooldown_s: float = 30.0
    otlp_endpoint: str = ""
    otlp_headers: str = ""


def load_config(env) -> Config:
    url = env.get("GATEWAY_UPSTREAM_URL", "").rstrip("/")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                f"GATEWAY_UPSTREAM_URL must be http(s) with a host: {url!r}"
            )
    return Config(
        url,
        env.get("GATEWAY_UPSTREAM_API_KEY", ""),
        env.get("GATEWAY_CLIENT_TOKEN", ""),
        env.get("GATEWAY_ROUTES_PATH", ""),
        env.get("GATEWAY_METRICS", "").lower() in ("on", "1", "true"),
        env.get("GATEWAY_CATALOG_PATH")
        or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "model-catalog.json"
        ),
        float(env.get("GATEWAY_COOLDOWN_S", "30")),
        env.get("GATEWAY_OTLP_ENDPOINT", ""),
        env.get("GATEWAY_OTLP_HEADERS", ""),
    )


_SERVICE_RE = re.compile(r"[a-z0-9._-]{1,64}")
MAX_SERVICES = 32
_now = time.monotonic  # patched by tests


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _parse_quota(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("quota must be an object")
    quota = {"window_s": raw.get("window_s", 60)}
    for key in ("requests", "tokens"):
        if key in raw:
            quota[key] = raw[key]
    if len(quota) == 1 or not all(_positive(v) for v in quota.values()):
        raise ValueError("quota needs positive window_s and requests and/or tokens")
    return quota


def _parse_upstream(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("upstream must be an object")
    url = raw.get("url")
    parsed = urlparse(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("upstream url must be http(s) with a host")
    for key in ("key_env", "model"):
        if key in raw and not (isinstance(raw[key], str) and raw[key]):
            raise ValueError(f"upstream {key} must be a non-empty string")
    return {
        "url": url.rstrip("/"),
        "key_env": raw.get("key_env"),
        "model": raw.get("model"),
    }


def _parse_routes(raw) -> dict:
    """Validate a routes file; raises ValueError when any object entry is malformed.

    A string value is the legacy form. Other scalar junk is ignored as before.
    """
    routes = {}
    for cls, value in raw.items():
        if cls not in ROUTE_CLASSES:
            continue
        if isinstance(value, dict):
            model = value.get("model")
            ups = value.get("upstreams", [])
            if not (isinstance(model, str) and model and isinstance(ups, list)):
                raise ValueError("route object needs a model and a list of upstreams")
            quota = _parse_quota(value["quota"]) if "quota" in value else None
            routes[cls] = (model, quota, tuple(_parse_upstream(u) for u in ups))
        elif isinstance(value, str) and value:
            routes[cls] = (value, None, ())
    return routes


def _usage(data: bytes):
    """(prompt, completion, response model) from a JSON or SSE body; tokens None when absent."""
    objs = []
    try:
        objs.append(json.loads(data))
    except (ValueError, RecursionError):
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("data:"):
                try:
                    objs.append(json.loads(line[5:]))
                except (ValueError, RecursionError):
                    pass
    usage = model = None
    for obj in objs:
        if isinstance(obj, dict):
            if isinstance(obj.get("model"), str):
                model = obj["model"]
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
    if not usage or not ({"prompt_tokens", "completion_tokens"} & usage.keys()):
        return None, None, model

    def count(value):
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )

    return (
        count(usage.get("prompt_tokens")),
        count(usage.get("completion_tokens")),
        model,
    )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _host(url: str) -> str:
    parsed = urlparse(url)  # host[:port] only: never userinfo, path or query
    return parsed.netloc.rpartition("@")[2]


class _Telemetry:
    """Best-effort OTLP/HTTP span exporter."""

    def __init__(self, endpoint: str = "", headers: str = ""):
        self.enabled = bool(endpoint)
        if not self.enabled:
            return
        self.url = endpoint.rstrip("/") + "/v1/traces"
        self.headers = {"Content-Type": "application/json"}
        for item in headers.split(","):
            if "=" in item:
                key, value = item.split("=", 1)
                self.headers[key.strip().replace("\r", "").replace("\n", "")] = (
                    value.strip().replace("\r", "").replace("\n", "")
                )
        self.queue = queue.Queue(maxsize=256)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def emit(self, attrs: dict, start_ns: int, end_ns: int, ok: bool) -> None:
        if not self.enabled:
            return
        attributes = []
        for key, value in attrs.items():
            if isinstance(value, bool):
                typed = {"boolValue": value}
            elif isinstance(value, int):
                typed = {"intValue": str(value)}
            elif isinstance(value, float):
                typed = {"doubleValue": value}
            else:
                typed = {"stringValue": value}
            attributes.append({"key": key, "value": typed})
        span = {
            "traceId": os.urandom(16).hex(),
            "spanId": os.urandom(8).hex(),
            "name": f"chat {attrs.get('factory.genai.route', '')}",
            "kind": 3,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": attributes,
            "status": {"code": 1 if ok else 2},
        }
        try:
            self.queue.put_nowait(span)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            span = self.queue.get()
            try:
                body = json.dumps(
                    {
                        "resourceSpans": [
                            {
                                "resource": {
                                    "attributes": [
                                        {
                                            "key": "service.name",
                                            "value": {"stringValue": "model-gateway"},
                                        }
                                    ]
                                },
                                "scopeSpans": [
                                    {
                                        "scope": {"name": "model-gateway"},
                                        "spans": [span],
                                    }
                                ],
                            }
                        ]
                    }
                ).encode()
                request = urllib.request.Request(
                    self.url, body, self.headers, method="POST"
                )
                with _opener.open(request, timeout=5):
                    pass
            except Exception:
                pass


class _State:
    """Quota windows, counters, service labels, upstream health. One lock, no I/O under it."""

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.Lock()
        self.counters: dict = {}
        self.windows: dict = {}
        self.services: set = set()
        self.down: dict = {}
        self.last_routes: dict = {}
        self.telemetry = _Telemetry(config.otlp_endpoint, config.otlp_headers)
        self.catalog = self._load_catalog(config.catalog_path)

    @staticmethod
    def _load_catalog(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as catalog_file:
                models = json.load(catalog_file).get("models")
        except (OSError, UnicodeError, ValueError, AttributeError, RecursionError):
            return {}
        return models if isinstance(models, dict) else {}

    def routes(self) -> dict:
        path = self.config.routes_path
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as routes_file:
                raw = json.load(routes_file)
            if not isinstance(raw, dict):
                raise ValueError("routes must be an object")
            parsed = _parse_routes(raw)
        except (OSError, UnicodeError, ValueError, RecursionError):
            # A bad reload must not drop a quota: keep the last valid file.
            with self.lock:
                return self.last_routes
        with self.lock:
            self.last_routes = parsed
        return parsed

    def service(self, raw) -> str:
        if not (isinstance(raw, str) and _SERVICE_RE.fullmatch(raw)):
            return "unknown"
        with self.lock:
            if raw not in self.services and len(self.services) >= MAX_SERVICES:
                return "other"
            self.services.add(raw)
        return raw

    def _window(self, cls: str, quota: dict, now: float) -> list:
        win = self.windows.get(cls)
        if win is None or win[3] != quota["window_s"] or now - win[0] >= win[3]:
            win = self.windows[cls] = [now, 0, 0, quota["window_s"]]
        return win

    def admit(self, cls: str, quota: dict) -> int:
        """0 when admitted (and counted), else Retry-After seconds."""
        now = _now()
        with self.lock:
            win = self._window(cls, quota, now)
            full = ("requests" in quota and win[1] >= quota["requests"]) or (
                "tokens" in quota and win[2] >= quota["tokens"]
            )
            if full:
                return max(1, math.ceil(win[0] + win[3] - now))
            win[1] += 1
        return 0

    def charge(self, cls: str, quota: dict, tokens: int) -> None:
        now = _now()
        with self.lock:
            win = self.windows.get(cls)
            if win is None or now - win[0] >= win[3]:
                win = self.windows[cls] = [now, 0, 0, quota["window_s"]]
            win[2] += tokens  # the window current at completion, whatever its length

    def inc(self, name: str, labels: dict, value: float = 1) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + value

    def order(self, ups: list) -> list:
        now = _now()
        with self.lock:
            healthy = [u for u in ups if self.down.get(u["url"], 0) <= now]
        return healthy + [u for u in ups if u not in healthy]

    def mark(self, url: str, failed: bool) -> None:
        with self.lock:
            if failed:
                self.down[url] = _now() + self.config.cooldown_s
            else:
                self.down.pop(url, None)

    def cost(self, model: str, prompt: int, completion: int):
        price = (self.catalog.get(model) or {}).get("price")
        if not (isinstance(price, dict) and price.get("mode") == "metered"):
            return None
        rate_in, rate_out = price.get("in_per_mtok"), price.get("out_per_mtok")
        if not (_positive(rate_in) and _positive(rate_out)):
            return None
        return (prompt * rate_in + completion * rate_out) / 1e6

    def render(self) -> str:
        now = _now()
        with self.lock:
            counters = sorted(self.counters.items())
            urls = {self.config.upstream_url} - {""}
            for _model, _quota, ups in self.last_routes.values():
                urls |= {u["url"] for u in ups}
            gauges = {_host(u): int(self.down.get(u, 0) <= now) for u in urls}
        lines, seen = [], set()
        for (name, labels), value in counters:
            if name not in seen:
                seen.add(name)
                lines.append(f"# TYPE {name} counter")
            body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
            lines.append(f"{name}{{{body}}} {value:g}")
        lines.append("# TYPE gateway_upstream_up gauge")
        lines += [
            f'gateway_upstream_up{{upstream="{_escape(h)}"}} {v}'
            for h, v in sorted(gauges.items())
        ]
        return "\n".join(lines) + "\n"


def _forward(url: str, key: str, body: bytes, ctype: str):
    """(status, content type, body), or None when the upstream is unreachable."""
    headers = {"Content-Type": ctype}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        url + "/v1/chat/completions", body, headers, method="POST"
    )
    # shortcut: the upstream response is buffered, not streamed; upgrade
    # when a caller needs SSE.
    try:
        with _opener.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as err:
        # Upstream error bodies are passed through as-is; some providers
        # quote a masked form of the key in them.
        try:
            return err.code, err.headers.get("Content-Type"), err.read()
        except (OSError, http.client.HTTPException):
            # The status is known: a 4xx stays terminal, a 5xx still fails over.
            return err.code, err.headers.get("Content-Type"), b""
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return None


def make_server(config: Config, host: str, port: int) -> ThreadingHTTPServer:
    state = _State(config)

    class Handler(BaseHTTPRequestHandler):
        timeout = CLIENT_TIMEOUT

        def _reply(self, status, body=b"", ctype="application/json", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra or {}).items():
                # CR/LF stripped so a header value can never split the response.
                self.send_header(name, value.replace("\r", "").replace("\n", ""))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, msg):
            self._reply(status, json.dumps({"error": msg}).encode())

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return hmac.compare_digest(
                auth.encode(), f"Bearer {config.client_token}".encode()
            )

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, b'{"status":"ok"}')
            elif self.path == "/metrics" and config.metrics:
                if not config.client_token:
                    self._error(503, "gateway not configured")
                elif not self._authorized():
                    self._error(401, "unauthorized")
                else:
                    self._reply(
                        200, state.render().encode(), "text/plain; version=0.0.4"
                    )
            else:
                self._error(404, "not found")

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                return self._error(404, "not found")
            if not (config.upstream_url and config.client_token):
                return self._error(503, "gateway not configured")
            if not self._authorized():
                return self._error(401, "unauthorized")
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length < 0:
                    raise ValueError
            except ValueError:
                return self._error(400, "bad Content-Length")
            if length > MAX_BODY:
                return self._error(413, "body too large")
            raw = self.rfile.read(length)
            start_ns, t0 = time.time_ns(), _now()
            routes = state.routes()
            payload = None
            if routes or config.metrics or state.telemetry.enabled:
                try:
                    parsed = json.loads(raw)
                except (ValueError, RecursionError):
                    parsed = None
                payload = parsed if isinstance(parsed, dict) else None
            requested = payload.get("model") if payload else None
            cls = (
                requested
                if isinstance(requested, str) and requested in routes
                else None
            )
            route_model, quota, route_ups = routes[cls] if cls else (None, None, ())
            label = cls or "direct"
            track = config.metrics or state.telemetry.enabled or bool(quota)
            service = state.service(self.headers.get("X-Caller-Service"))
            labels = {"route": label, "service": service}
            attrs = {
                "factory.genai.route": label,
                "factory.genai.caller_service": service,
                "gen_ai.operation.name": "chat",
            }
            if isinstance(requested, str):
                attrs["gen_ai.request.model"] = requested

            def finish(outcome, status, **extra):
                attrs["factory.genai.outcome"] = outcome
                attrs["factory.genai.latency_ms"] = int((_now() - t0) * 1000)
                if config.metrics:
                    state.inc("gateway_requests_total", {**labels, "outcome": outcome})
                state.telemetry.emit(attrs, start_ns, time.time_ns(), outcome == "ok")
                return status

            if quota:
                retry = state.admit(cls, quota)
                if retry:
                    finish("quota", 429)
                    self._reply(
                        429,
                        b'{"error":"route quota exceeded"}',
                        extra={"Retry-After": str(retry), "X-Model-Route": cls},
                    )
                    return None
            if route_ups:
                ups = [
                    {
                        "url": u["url"],
                        "key": os.environ.get(u["key_env"], "") if u["key_env"] else "",
                        "model": u["model"] or route_model,
                    }
                    for u in route_ups
                ]
            else:
                ups = [
                    {
                        "url": config.upstream_url,
                        "key": config.upstream_key,
                        "model": route_model,
                    }
                ]
            ctype = self.headers.get("Content-Type", "application/json")
            order = state.order(ups)
            failed_from = result = used = None
            for i, up in enumerate(order):
                body = raw
                if payload is not None:
                    sent = dict(payload)
                    if up["model"]:
                        sent["model"] = up["model"]
                    if (
                        track
                        and sent.get("stream") is True
                        and "stream_options" not in sent
                    ):
                        sent["stream_options"] = {"include_usage": True}
                    if sent != payload:
                        body = json.dumps(sent).encode()
                result = _forward(up["url"], up["key"], body, ctype)
                used = up
                failed = result is None or result[0] >= 500
                state.mark(up["url"], failed)
                if not (failed and i < len(order) - 1):
                    break
                failed_from = failed_from or up["url"]
                if config.metrics:
                    state.inc("gateway_failovers_total", {"route": label})
            attrs["factory.genai.provider"] = _host(used["url"])
            if failed_from:
                attrs["factory.genai.failover_from"] = _host(failed_from)
            if result is None:
                finish("upstream_error", 502)
                return self._error(502, "upstream unreachable")
            status, rtype, data = result
            model = used["model"] or requested
            if isinstance(model, str):
                attrs["factory.genai.resolved_model"] = model
            outcome = (
                "ok"
                if status < 400
                else "client_error"
                if status < 500
                else "upstream_error"
            )
            if status < 300 and track:
                prompt, completion, resp_model = _usage(data)
                if resp_model:
                    attrs["gen_ai.response.model"] = resp_model
                if prompt is None:
                    if config.metrics:
                        state.inc("gateway_usage_missing_total", {"route": label})
                else:
                    attrs["factory.genai.prompt_tokens"] = attrs[
                        "gen_ai.usage.input_tokens"
                    ] = prompt
                    attrs["factory.genai.completion_tokens"] = attrs[
                        "gen_ai.usage.output_tokens"
                    ] = completion
                    cost = (
                        state.cost(model, prompt, completion)
                        if isinstance(model, str)
                        else None
                    )
                    if cost is not None:
                        attrs["factory.genai.cost_usd"] = cost
                    if config.metrics:
                        state.inc(
                            "gateway_tokens_total", {**labels, "kind": "prompt"}, prompt
                        )
                        state.inc(
                            "gateway_tokens_total",
                            {**labels, "kind": "completion"},
                            completion,
                        )
                        state.inc("gateway_cost_usd_total", labels, cost or 0)
                        if cost is None:
                            state.inc("gateway_unpriced_total", {"route": label})
                    if quota:
                        state.charge(cls, quota, prompt + completion)
            finish(outcome, status)
            extra = {}
            if cls:
                extra["X-Model-Route"] = cls
            if failed_from:
                extra["X-Model-Failover-From"] = _host(failed_from)
            return self._reply(status, data, rtype or "application/json", extra or None)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.state = state
    return server


def _self_test() -> None:
    """Loopback end to end: a priced fake upstream must yield non-zero request, token and cost counters."""
    import tempfile

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(
                {
                    "model": "selftest",
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    with tempfile.TemporaryDirectory() as tmp:
        catalog = os.path.join(tmp, "catalog.json")
        with open(catalog, "w", encoding="utf-8") as catalog_file:
            json.dump(
                {
                    "models": {
                        "selftest": {
                            "price": {
                                "mode": "metered",
                                "in_per_mtok": 1,
                                "out_per_mtok": 2,
                            }
                        }
                    }
                },
                catalog_file,
            )
        env = {
            "GATEWAY_UPSTREAM_URL": f"http://127.0.0.1:{up.server_address[1]}",
            "GATEWAY_CLIENT_TOKEN": "selftest",
            "GATEWAY_METRICS": "on",
            "GATEWAY_CATALOG_PATH": catalog,
        }
        gw = make_server(load_config(env), "127.0.0.1", 0)
        for srv in (up, gw):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{gw.server_address[1]}"
        auth = {"Authorization": "Bearer selftest"}
        try:
            req = urllib.request.Request(
                base + "/v1/chat/completions",
                b'{"model":"selftest"}',
                {**auth, "X-Caller-Service": "selftest"},
            )
            urllib.request.urlopen(req, timeout=5).read()  # noqa: S310
            req = urllib.request.Request(base + "/metrics", headers=auth)
            text = urllib.request.urlopen(req, timeout=5).read().decode()  # noqa: S310
        finally:
            gw.shutdown()
            up.shutdown()
    for name in (
        "gateway_requests_total",
        "gateway_tokens_total",
        "gateway_cost_usd_total",
    ):
        total = sum(
            float(line.rpartition(" ")[2])
            for line in text.splitlines()
            if line.startswith(name + "{")
        )
        if total <= 0:
            raise SystemExit(f"self-test failed: {name} is zero\n{text}")
    print("self-test ok")


def main() -> None:
    if "--self-test" in sys.argv:
        return _self_test()
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    make_server(load_config(os.environ), "0.0.0.0", port).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
