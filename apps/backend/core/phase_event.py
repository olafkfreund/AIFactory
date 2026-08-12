"""
Execution phase event protocol for frontend synchronization.

Protocol: __EXEC_PHASE__:{"phase":"coding","message":"Starting"}
"""

import json
import os
import sys
import time
from enum import Enum
from typing import Any

PHASE_MARKER_PREFIX = "__EXEC_PHASE__:"
_DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


class ExecutionPhase(str, Enum):
    """Maps to frontend's ExecutionPhase type for task card badges."""

    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"  # Paused for human plan approval
    CODING = "coding"
    QA_REVIEW = "qa_review"
    QA_FIXING = "qa_fixing"
    COMPLETE = "complete"
    FAILED = "failed"


def emit_phase(
    phase: ExecutionPhase | str,
    message: str = "",
    *,
    progress: int | None = None,
    subtask: str | None = None,
) -> None:
    """Emit structured phase event to stdout for frontend parsing."""
    phase_value = phase.value if isinstance(phase, ExecutionPhase) else phase

    payload: dict[str, Any] = {
        "phase": phase_value,
        "message": message,
    }

    if progress is not None:
        if not (0 <= progress <= 100):
            progress = max(0, min(100, progress))
        payload["progress"] = progress

    if subtask is not None:
        payload["subtask"] = subtask

    try:
        print(f"{PHASE_MARKER_PREFIX}{json.dumps(payload, default=str)}", flush=True)
    except (OSError, UnicodeEncodeError) as e:
        if _DEBUG:
            print(f"[phase_event] emit failed: {e}", file=sys.stderr, flush=True)


# Live token-usage protocol: __USAGE__:{"totalInputTokens":…,"outputTokens":…}
#
# Same channel and same reasoning as the phase marker above (#1229): under the
# kubejob backend the build runs in a Job whose /work is an ephemeral emptyDir,
# so token_usage.json does not reach the control plane until it is pushed back
# at the very END. The stdout stream, however, is followed live. Emitting the
# aggregate here is what makes accruing cost visible WHILE a build runs (#1249).
USAGE_MARKER_PREFIX = "__USAGE__:"

# Attribution folds a turn on every model response, which is far more often than
# a cockpit needs to redraw a cost tile. Throttle per process; the control plane
# is a consumer of whatever arrives, so the rate limit belongs here at the source.
_USAGE_EMIT_INTERVAL_S = 15.0
# Seeded so the FIRST emit is always allowed. The clock is time.monotonic(),
# whose zero is an arbitrary point (boot, on Linux) -- not "long ago". Seeding
# 0.0 meant that in a process starting within 15s of boot, `now - 0.0` is under
# the interval and the opening usage marker was thrown away. That is exactly the
# build whose cost tile a cockpit is waiting on.
_last_usage_emit: float = float("-inf")


def emit_usage(aggregate: dict[str, Any], *, force: bool = False) -> bool:
    """Emit the running token/cost aggregate to stdout. Returns True if emitted.

    Best-effort and non-fatal by construction: cost reporting must never be able
    to break a build. ``force`` bypasses the throttle for a final emit.
    """
    global _last_usage_emit  # noqa: PLW0603 - module-level throttle clock

    if not isinstance(aggregate, dict):
        return False
    now = time.monotonic()
    if not force and (now - _last_usage_emit) < _USAGE_EMIT_INTERVAL_S:
        return False
    try:
        print(  # noqa: T201 - stdout IS the transport for this protocol
            f"{USAGE_MARKER_PREFIX}{json.dumps(aggregate, default=str)}",
            flush=True,
        )
    except (OSError, UnicodeEncodeError, TypeError, ValueError) as e:
        if _DEBUG:
            print(  # noqa: T201 - debug-only, matches emit_phase above
                f"[phase_event] usage emit failed: {e}", file=sys.stderr, flush=True
            )
        return False
    # Only a real emit advances the clock, so an early failure keeps retrying
    # cheaply rather than starting a 15s dead window.
    _last_usage_emit = now
    return True
