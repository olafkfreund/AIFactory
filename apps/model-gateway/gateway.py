"""Minimal in-house model gateway (Factory#1959). Stdlib only.

Forwards ``POST /v1/chat/completions`` to an OpenAI-compatible upstream,
swapping the caller's bearer token for the upstream API key.

Env: GATEWAY_UPSTREAM_URL, GATEWAY_UPSTREAM_API_KEY (optional),
GATEWAY_CLIENT_TOKEN, GATEWAY_PORT (default 8080).
"""

from __future__ import annotations

import hmac
import json
import os
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
    )


def make_server(config: Config, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = CLIENT_TIMEOUT

        def _reply(self, status, body=b"", ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, msg):
            self._reply(status, json.dumps({"error": msg}).encode())

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, b'{"status":"ok"}')
            else:
                self._error(404, "not found")

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                return self._error(404, "not found")
            if not (config.upstream_url and config.client_token):
                return self._error(503, "gateway not configured")
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(
                auth.encode(), f"Bearer {config.client_token}".encode()
            ):
                return self._error(401, "unauthorized")
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length < 0:
                    raise ValueError
            except ValueError:
                return self._error(400, "bad Content-Length")
            if length > MAX_BODY:
                return self._error(413, "body too large")
            headers = {
                "Content-Type": self.headers.get("Content-Type", "application/json")
            }
            if config.upstream_key:
                headers["Authorization"] = f"Bearer {config.upstream_key}"
            req = urllib.request.Request(
                config.upstream_url + "/v1/chat/completions",
                self.rfile.read(length),
                headers,
                method="POST",
            )
            # shortcut: the upstream response is buffered, not streamed; upgrade
            # when a caller needs SSE.
            try:
                with _opener.open(req, timeout=UPSTREAM_TIMEOUT) as resp:
                    status, ctype, body = (
                        resp.status,
                        resp.headers.get("Content-Type"),
                        resp.read(),
                    )
            except urllib.error.HTTPError as err:
                # Upstream error bodies are passed through as-is; some providers
                # quote a masked form of the key in them.
                try:
                    body = err.read()
                except OSError:
                    return self._error(502, "upstream unreachable")
                status, ctype = err.code, err.headers.get("Content-Type")
            except (urllib.error.URLError, OSError):
                return self._error(502, "upstream unreachable")
            self._reply(status, body, ctype or "application/json")

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    make_server(load_config(os.environ), "0.0.0.0", port).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
