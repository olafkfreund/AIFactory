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
    """Wire stdlib logging → structlog → JSON-to-stdout.

    Idempotent: re-configuring is a no-op for callers (structlog's
    ``configure`` replaces the processor chain wholesale).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

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
