"""HTTP client used by the standalone MCP server's task-control tools.

Sole purpose: let MCP tools (running as a stdio subprocess of the user's
Claude Code session) call into the AIFactory web-server's REST API to
list/inspect/drive tasks.

Why a separate client (not just ``httpx.AsyncClient`` inline):
- One place for the bearer-token chain (env override → ``~/.aifactory/.token``)
  so token rotation works without restarting the MCP subprocess.
- One place for the friendly-error mapping so every tool returns the
  same operator guidance when the web-server is down, the token is
  rejected, or the server returns 5xx.
- Lazy-initialized client so a bare ``--help`` on the server doesn't
  open a connection pool.

Per the Epic #50 design: tools have full admin via the legacy bearer
token at ``~/.aifactory/.token``. Per-user tokens land in the v1.1
RBAC work; this client gets a token rotation when those mint flows go
live.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]


DEFAULT_API_URL = "http://localhost:3101"
DEFAULT_TOKEN_FILE = "~/.aifactory/.token"
DEFAULT_TIMEOUT = 30.0


class MCPHTTPError(RuntimeError):
    """Operator-actionable error from the MCP HTTP client.

    The string form is what the tool surfaces in its ``content[0].text``
    response — keep it single-line, no stack traces, and end with a
    concrete next step.
    """


class _ClientState:
    """Lazy singleton — opened on first request, reused thereafter."""

    def __init__(self) -> None:
        self._client: Any = None  # httpx.AsyncClient | None
        self._base_url: str | None = None

    def base_url(self) -> str:
        # Re-evaluated each call so an operator can change AIFACTORY_API_URL
        # in a running shell without restarting the MCP subprocess.
        return os.environ.get("AIFACTORY_API_URL", DEFAULT_API_URL).rstrip("/")

    async def get_client(self) -> Any:
        if not HTTPX_AVAILABLE:
            raise MCPHTTPError(
                "httpx not installed in the MCP subprocess venv — "
                "install it with: pip install httpx"
            )
        base = self.base_url()
        if self._client is None or self._base_url != base:
            if self._client is not None:
                await self._client.aclose()
            self._client = httpx.AsyncClient(base_url=base, timeout=DEFAULT_TIMEOUT)
            self._base_url = base
        return self._client


_state = _ClientState()


def _read_token() -> str:
    """Return the bearer token from $AIFACTORY_API_TOKEN_FILE or the default.

    Re-read at every call so operators can rotate the token (regenerate via
    the web UI, write the file, no restart needed) without redeploying the
    MCP subprocess. Never echo this value in error messages.
    """
    token_path = Path(
        os.environ.get("AIFACTORY_API_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    ).expanduser()
    if not token_path.exists():
        raise MCPHTTPError(
            f"AIFactory API token not found at {token_path} — "
            "regenerate via the web UI or run: python -m server.main"
        )
    try:
        token = token_path.read_text().strip()
    except OSError as exc:
        raise MCPHTTPError(
            f"Cannot read AIFactory token at {token_path}: {exc}"
        ) from exc
    if not token:
        raise MCPHTTPError(
            f"AIFactory token at {token_path} is empty — regenerate via the web UI"
        )
    return token


async def request(method: str, path: str, **kwargs: Any) -> dict[str, Any] | list:
    """Make an authenticated request against the AIFactory web-server.

    ``kwargs`` are forwarded to ``httpx.AsyncClient.request`` (e.g.
    ``params=``, ``json=``). The bearer token is added to ``headers``;
    any caller-supplied ``Authorization`` header is overridden — single
    auth path for the MCP control plane.

    Returns the parsed JSON body on success. Raises ``MCPHTTPError`` with
    operator-actionable single-line guidance on failure:
    - Connection refused → "web-server not reachable, start with: ..."
    - 401 → "token rejected, regenerate via web UI"
    - 5xx → "server error: <truncated body>"
    """
    if not HTTPX_AVAILABLE:
        raise MCPHTTPError(
            "httpx not installed in the MCP subprocess venv — "
            "install it with: pip install httpx"
        )

    token = _read_token()
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token}"

    client = await _state.get_client()
    base = _state.base_url()

    try:
        response = await client.request(method, path, headers=headers, **kwargs)
    except httpx.ConnectError as exc:
        raise MCPHTTPError(
            f"AIFactory web-server not reachable at {base} — "
            "start it with: python -m server.main"
        ) from exc
    except httpx.TimeoutException as exc:
        raise MCPHTTPError(
            f"AIFactory web-server at {base} timed out after {DEFAULT_TIMEOUT}s"
        ) from exc

    if response.status_code == 401:
        token_path = os.environ.get("AIFACTORY_API_TOKEN_FILE", DEFAULT_TOKEN_FILE)
        raise MCPHTTPError(
            f"AIFactory token at {token_path} rejected — regenerate via the web UI"
        )
    if response.status_code == 404:
        # Tools may want to differentiate "no such resource" from other
        # errors; surface a structured message but stay a single line.
        raise MCPHTTPError(
            f"Resource not found at {method} {path} (HTTP 404)"
        )
    if response.status_code >= 500:
        body = response.text[:500]
        raise MCPHTTPError(
            f"AIFactory web-server returned HTTP {response.status_code}: {body}"
        )
    if response.status_code >= 400:
        body = response.text[:500]
        raise MCPHTTPError(
            f"AIFactory web-server returned HTTP {response.status_code}: {body}"
        )

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise MCPHTTPError(
            f"AIFactory web-server returned non-JSON body: {response.text[:200]}"
        ) from exc


async def reset() -> None:
    """Close the underlying client. Test/CLI helper, not used at runtime."""
    if _state._client is not None:
        await _state._client.aclose()
        _state._client = None
        _state._base_url = None
