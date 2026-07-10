"""Correlation ID propagation (Epic #26 P6.2).

X-Request-ID is the de-facto header for HTTP-tier request tracing.
This middleware:
  - Reads X-Request-ID from the incoming request; auto-generates a
    UUID if absent.
  - Stashes the value in a contextvar (read by structlog's
    _add_request_id processor + by outbound httpx via the
    install_httpx_propagation hook).
  - Echoes the ID back in the response so the caller can correlate
    their request to our log lines.

The contextvar pattern means correlation IDs propagate through
``await`` calls within the request without explicit threading.
"""

from __future__ import annotations

import contextvars
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

CORRELATION_ID_HEADER = "X-Request-ID"

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aifactory.request_id", default=None
)


def get_correlation_id() -> str | None:
    """Return the current request's ID, or None outside a request scope."""
    return _correlation_id.get()


def set_correlation_id(rid: str | None) -> contextvars.Token:
    """Test / library hook to set the contextvar; returns the reset token."""
    return _correlation_id.set(rid)


def reset_correlation_id(token: contextvars.Token) -> None:
    _correlation_id.reset(token)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that ensures every request carries a correlation ID.

    Order matters: install BEFORE the auth middleware so even
    401-rejected requests carry the ID in their response (auditors
    rely on this to trace failed auth attempts).
    """

    async def dispatch(self, request: Request, call_next):
        # Resolution order:
        #   1. Client-provided X-Request-ID — explicit caller intent,
        #      always wins (operators correlate across services with
        #      their own IDs).
        #   2. Active OTel span's trace_id — when FastAPI's OTel
        #      instrumentor has already opened a request span, use
        #      that 32-hex trace_id so logs + traces share an ID.
        #   3. Fresh UUID — fallback when neither exists.
        # See Epic #35 #42 design doc.
        rid = (
            request.headers.get(CORRELATION_ID_HEADER)
            or _try_trace_id()
            or str(uuid.uuid4())
        )
        token = _correlation_id.set(rid)
        try:
            response = await call_next(request)
        finally:
            _correlation_id.reset(token)
        response.headers[CORRELATION_ID_HEADER] = rid
        return response


def _try_trace_id() -> str | None:
    """Return the current OTel span's 32-hex trace_id, or None when
    no span is active / OTel isn't installed. Failure-safe: any
    exception (import error, missing context) yields None so the
    fallback path runs."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None:
            return None
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return None
        return f"{ctx.trace_id:032x}"
    except Exception:
        return None


def install_httpx_propagation() -> None:
    """Patch httpx clients to forward the current correlation ID.

    Idempotent. Applies to BOTH httpx.Client and httpx.AsyncClient at
    module level — every client instance created after this call
    propagates the contextvar's X-Request-ID on outgoing requests.

    The wrapping uses an event hook so application code doesn't need
    to know correlation IDs exist.
    """
    import httpx

    if getattr(httpx, "_aifactory_corr_id_installed", False):
        return

    def _add_correlation_header(request):
        rid = get_correlation_id()
        if rid and CORRELATION_ID_HEADER not in request.headers:
            request.headers[CORRELATION_ID_HEADER] = rid

    # Wrap Client and AsyncClient __init__ to append the hook.
    _orig_client_init = httpx.Client.__init__
    _orig_aclient_init = httpx.AsyncClient.__init__

    def _client_init(self, *args, **kwargs):
        event_hooks = kwargs.setdefault("event_hooks", {})
        req_hooks = list(event_hooks.get("request") or [])
        req_hooks.append(_add_correlation_header)
        event_hooks["request"] = req_hooks
        _orig_client_init(self, *args, **kwargs)

    def _aclient_init(self, *args, **kwargs):
        event_hooks = kwargs.setdefault("event_hooks", {})
        req_hooks = list(event_hooks.get("request") or [])

        # AsyncClient hooks must be coroutines.
        async def _ahook(request):
            _add_correlation_header(request)

        req_hooks.append(_ahook)
        event_hooks["request"] = req_hooks
        _orig_aclient_init(self, *args, **kwargs)

    httpx.Client.__init__ = _client_init
    httpx.AsyncClient.__init__ = _aclient_init
    httpx._aifactory_corr_id_installed = True
