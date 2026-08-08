"""
Shared Error Utilities
======================

Common error detection and classification functions used across
agent sessions, QA, and other modules.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_agent_sdk.types import Message

logger = logging.getLogger(__name__)

# Default cooldown (seconds) used when a rate limit is detected but no explicit
# retry-after / reset time can be parsed from the error. Conservative so we do
# not hammer the provider, short enough that builds recover promptly.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60.0

# Hard ceiling on any single parsed cooldown so a malformed / far-future reset
# timestamp can never wedge a build for hours.
MAX_RATE_LIMIT_COOLDOWN_SECONDS = 3600.0


def is_tool_concurrency_error(error: Exception) -> bool:
    """
    Check if an error is a 400 tool concurrency error from Claude API.

    Tool concurrency errors occur when too many tools are used simultaneously
    in a single API request, hitting Claude's concurrent tool use limit.

    Args:
        error: The exception to check

    Returns:
        True if this is a tool concurrency error, False otherwise
    """
    error_str = str(error).lower()
    # Check for 400 status AND tool concurrency keywords
    return "400" in error_str and (
        ("tool" in error_str and "concurrency" in error_str)
        or "too many tools" in error_str
        or "concurrent tool" in error_str
    )


def is_rate_limit_error(error: Exception) -> bool:
    """
    Check if an error is a rate limit error (429 or similar).

    Rate limit errors occur when the API usage quota is exceeded,
    either for session limits or weekly limits.

    Args:
        error: The exception to check

    Returns:
        True if this is a rate limit error, False otherwise
    """
    error_str = str(error).lower()

    # Check for HTTP 429 with word boundaries to avoid false positives
    if re.search(r"\b429\b", error_str):
        return True

    # Check for other rate limit indicators
    return any(
        p in error_str
        for p in [
            "limit reached",
            "rate limit",
            "too many requests",
            "usage limit",
            "quota exceeded",
        ]
    )


# Patterns for extracting a cooldown from a rate-limit error. Ordered most- to
# least-specific. Each pattern captures a single numeric/timestamp group.
#   retry-after: <n>            (HTTP header style, seconds)
#   retry in <n>s / <n> seconds (SDK / human-readable)
#   x-ratelimit-reset: <epoch>  (unix reset timestamp)
#   reset(s)? (at|in) <n>       (generic)
_RETRY_AFTER_SECONDS_RE = re.compile(
    r"retry[\s_-]*after[\"'\s:=]+(\d+(?:\.\d+)?)", re.IGNORECASE
)
_RETRY_IN_SECONDS_RE = re.compile(
    r"retry(?:[\s_-]*in)?[\"'\s:=]+(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?\b",
    re.IGNORECASE,
)
_RESET_EPOCH_RE = re.compile(
    r"(?:x-)?rate[\s_-]*limit[\s_-]*reset[\"'\s:=]+(\d{9,})", re.IGNORECASE
)
# ISO-8601 reset timestamp, e.g. resets_at: 2026-06-02T12:30:00Z
_RESET_ISO_RE = re.compile(
    r"reset[a-z_]*[\"'\s:=]+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)


def _clamp_cooldown(seconds: float) -> float:
    """Clamp a parsed cooldown into the sane [0, MAX] range."""
    if seconds < 0:
        return 0.0
    return min(seconds, MAX_RATE_LIMIT_COOLDOWN_SECONDS)


def _parse_iso_reset_to_cooldown(value: str, now: float) -> float | None:
    """Convert an ISO-8601 reset timestamp into remaining cooldown seconds."""
    text = value.strip()
    # datetime.fromisoformat handles "+00:00" but not a trailing "Z" on <3.11
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt.timestamp() - now
    return delta if delta > 0 else 0.0


def extract_rate_limit_cooldown(
    error: Any,
    *,
    default_cooldown: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    now: float | None = None,
) -> float:
    """Extract the cooldown (seconds to wait) from a rate-limit error/result.

    Pure and provider-defensive. Inspects, in priority order:
      1. A ``retry_after`` / ``retry_after_seconds`` attribute or dict key
         (Anthropic SDK, github errors, OpenAI-style responses).
      2. A ``retry-after``/``retry in <n>s`` substring in the error text.
      3. A unix-epoch ``x-ratelimit-reset`` value in the text.
      4. An ISO-8601 reset timestamp in the text.

    When the input is a rate limit but no explicit time can be parsed, returns
    ``default_cooldown``. The result is always clamped to
    ``[0, MAX_RATE_LIMIT_COOLDOWN_SECONDS]``.

    Args:
        error: Exception, dict, or any object whose ``str()`` / attributes may
            carry rate-limit timing.
        default_cooldown: Fallback when only the *fact* of a rate limit is known.
        now: Injectable current unix time (for epoch/ISO math); defaults to
            ``time.time()``.

    Returns:
        Non-negative float seconds to wait before resuming.
    """
    now = time.time() if now is None else now

    # 1. Structured attributes / dict keys (most reliable).
    candidate: Any = None
    for key in ("retry_after", "retry_after_seconds"):
        if isinstance(error, dict) and key in error:
            candidate = error[key]
        else:
            candidate = getattr(error, key, None)
        if candidate is not None:
            try:
                return _clamp_cooldown(float(candidate))
            except (TypeError, ValueError):
                candidate = None

    # Structured epoch reset attribute / key.
    for key in ("reset_at", "reset", "x-ratelimit-reset"):
        raw = error.get(key) if isinstance(error, dict) else getattr(error, key, None)
        if raw is None:
            continue
        try:
            reset_epoch = float(raw)
        except (TypeError, ValueError):
            continue
        # Heuristic: values that look like a unix timestamp are absolute.
        if reset_epoch > 1_000_000_000:
            return _clamp_cooldown(reset_epoch - now)
        if reset_epoch >= 0:
            return _clamp_cooldown(reset_epoch)

    # 2-4. Fall back to scraping the stringified error.
    text = str(error)

    m = _RETRY_AFTER_SECONDS_RE.search(text)
    if m:
        return _clamp_cooldown(float(m.group(1)))

    m = _RETRY_IN_SECONDS_RE.search(text)
    if m:
        return _clamp_cooldown(float(m.group(1)))

    m = _RESET_EPOCH_RE.search(text)
    if m:
        return _clamp_cooldown(float(m.group(1)) - now)

    m = _RESET_ISO_RE.search(text)
    if m:
        parsed = _parse_iso_reset_to_cooldown(m.group(1), now)
        if parsed is not None:
            return _clamp_cooldown(parsed)

    # Known rate limit, but no parseable timing — use the default.
    return _clamp_cooldown(default_cooldown)


@dataclass(frozen=True)
class RateLimitResumePolicy:
    """Bounded backoff policy for auto-resuming a rate-limited session.

    All knobs are caps, not behaviours — the policy never *invents* a longer
    wait than the provider asked for; it only refuses to resume once a cap is
    exceeded.

    Attributes:
        max_retries: Maximum number of resume attempts before giving up.
        max_total_wait_seconds: Cumulative wait budget across all resumes.
        min_cooldown_seconds: Floor applied to any cooldown (avoids busy-loops
            when a provider reports ``retry_after: 0``).
        jitter_ratio: Fraction of the cooldown added as random jitter (0..1) to
            avoid thundering-herd resumes across parallel builds.
    """

    max_retries: int = 5
    max_total_wait_seconds: float = 1800.0
    min_cooldown_seconds: float = 1.0
    jitter_ratio: float = 0.1


@dataclass(frozen=True)
class ResumeDecision:
    """Outcome of a resume-policy evaluation.

    Attributes:
        should_resume: Whether the caller should wait and re-invoke.
        wait_seconds: How long to sleep before resuming (0 if not resuming).
        reason: Human-readable explanation for logs.
    """

    should_resume: bool
    wait_seconds: float
    reason: str


def decide_rate_limit_resume(
    *,
    cooldown_seconds: float,
    attempt: int,
    elapsed_wait_seconds: float,
    policy: RateLimitResumePolicy | None = None,
    rng: Callable[[], float] | None = None,
) -> ResumeDecision:
    """Decide whether (and how long) to wait before resuming after a rate limit.

    Pure and deterministic given its inputs (inject ``rng`` for reproducible
    jitter in tests).

    Args:
        cooldown_seconds: Provider-reported / default cooldown for this hit.
        attempt: 1-based index of this resume attempt (1 = first auto-resume).
        elapsed_wait_seconds: Total seconds already spent waiting on prior
            rate-limit resumes for this build.
        policy: Caps to enforce; defaults to ``RateLimitResumePolicy()``.
        rng: Zero-arg callable returning a float in [0, 1) for jitter; defaults
            to ``random.random``.

    Returns:
        A :class:`ResumeDecision`.
    """
    policy = policy or RateLimitResumePolicy()
    rng = random.random if rng is None else rng

    if attempt > policy.max_retries:
        return ResumeDecision(
            should_resume=False,
            wait_seconds=0.0,
            reason=(f"max retries exceeded (attempt {attempt} > {policy.max_retries})"),
        )

    base = max(cooldown_seconds, policy.min_cooldown_seconds)
    jitter = base * policy.jitter_ratio * rng()
    wait = base + jitter

    if elapsed_wait_seconds + wait > policy.max_total_wait_seconds:
        return ResumeDecision(
            should_resume=False,
            wait_seconds=0.0,
            reason=(
                f"total wait budget exceeded ("
                f"{elapsed_wait_seconds + wait:.0f}s > "
                f"{policy.max_total_wait_seconds:.0f}s)"
            ),
        )

    return ResumeDecision(
        should_resume=True,
        wait_seconds=wait,
        reason=(
            f"resuming after {wait:.0f}s cooldown "
            f"(attempt {attempt}/{policy.max_retries})"
        ),
    )


def is_authentication_error(error: Exception) -> bool:
    """
    Check if an error is an authentication error (401, token expired, etc.).

    Authentication errors occur when OAuth tokens are invalid, expired,
    or have been revoked (e.g., after token refresh on another process).

    Args:
        error: The exception to check

    Returns:
        True if this is an authentication error, False otherwise
    """
    error_str = str(error).lower()

    # Check for HTTP 401 with word boundaries to avoid false positives
    if re.search(r"\b401\b", error_str):
        return True

    # Check for other authentication indicators
    return any(
        p in error_str
        for p in [
            "authentication failed",
            "authentication error",
            "unauthorized",
            "invalid token",
            "token expired",
            "authentication_error",
            "invalid_token",
            "token_expired",
            "not authenticated",
            "http 401",
            "does not have access to claude",
            "please login again",
        ]
    )


async def safe_receive_messages(
    client,
    *,
    caller: str = "agent",
) -> AsyncIterator[Message]:
    """Iterate over SDK messages with resilience against unexpected errors.

    The SDK's ``receive_response()`` async generator can terminate early if:
    1. An unhandled message type slips past the monkey-patch.
    2. A transient parse error corrupts a single message in the stream.
    3. An unexpected ``StopAsyncIteration`` or runtime error occurs mid-stream.

    This wrapper catches per-message errors, logs them, and continues yielding
    subsequent messages so the agent session can complete its work.

    It also detects rate-limit events (surfaced as ``SystemMessage`` with
    subtype ``unknown_rate_limit_event``) and logs a user-visible warning.

    Args:
        client: A ``ClaudeSDKClient`` instance (must be inside ``async with``).
        caller: Label for log messages (e.g., "session", "agent_runner").

    Yields:
        Parsed ``Message`` objects from the SDK response stream.
    """
    try:
        async for msg in client.receive_response():
            # Detect rate-limit events surfaced by the monkey-patch
            msg_type = type(msg).__name__
            if msg_type == "SystemMessage":
                subtype = getattr(msg, "subtype", "")
                if subtype.startswith("unknown_"):
                    original_type = subtype[len("unknown_") :]
                    if "rate_limit" in original_type:
                        data = getattr(msg, "data", {})
                        retry_after = data.get("retry_after") or data.get(
                            "data", {}
                        ).get("retry_after")
                        retry_info = (
                            f" (retry in {retry_after}s)" if retry_after else ""
                        )
                        logger.warning(f"[{caller}] Rate limit event{retry_info}")
                    else:
                        logger.debug(
                            f"[{caller}] Skipping unknown SDK message type: {original_type}"
                        )
                    continue
            yield msg
    except GeneratorExit:
        return
    except Exception as e:
        # If the generator itself raises (e.g., transport error), log and stop
        # gracefully so callers can process whatever was collected so far.
        logger.error(f"[{caller}] SDK response stream terminated unexpectedly: {e}")
        return
