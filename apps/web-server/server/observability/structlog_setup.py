"""structlog → JSON to stdout (Epic #26 P6.1).

Banks ship application logs to Loki / ELK / Splunk via stdout. JSON
output is the lingua franca — operators parse with jq or pipe to the
shipper. We pin a small, stable set of fields:

  timestamp  — ISO-8601 UTC
  level      — log level name (info / warning / etc.)
  logger     — logger name (the dotted module path)
  event      — the log message
  request_id — correlation ID (P6.2); absent for non-request logs

structlog's bind() lets us decorate every log line emitted in a
request scope with the correlation ID (set by CorrelationIdMiddleware).
"""

from __future__ import annotations

import logging
import sys

import structlog


def _add_request_id(_logger, _name, event_dict):
    """Processor: inject the contextvar-stashed correlation ID."""
    # Lazy import to break the cycle (correlation_id imports structlog).
    from .correlation_id import get_correlation_id

    rid = get_correlation_id()
    if rid is not None:
        event_dict["request_id"] = rid
    return event_dict


def configure_structlog(level: str = "INFO") -> None:
    """Wire structlog → JSON-to-stdout.

    Idempotent: re-configuring is a no-op for callers (structlog's
    ``configure`` replaces the processor chain wholesale).

    Deliberately does NOT call ``logging.basicConfig(force=True)``.
    Earlier versions did, which silently nuked the file handlers
    ``setup_logging`` had installed seconds before for
    ``server.log`` / ``errors.log`` / ``agent.log`` — every line
    emitted after this call vanished to stdout instead of reaching
    the rotating file handlers operators monitor.  structlog still
    writes via its own ``PrintLoggerFactory`` to stdout; the stdlib
    loggers keep their file handlers untouched.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    # Only set the root level if no handler is attached, so we don't
    # override an embedding test's logging config.
    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(log_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _add_request_id,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    """Return a structlog-bound logger. Same call shape as logging.getLogger."""
    return structlog.get_logger(name)
