"""
Self-Heal Loop (Phase 3 of the self-healing pipeline; tracking #397)
===================================================================

The bounded-autonomy policy the user chose: around each unit of work
(subtask / wave), checkpoint → attempt → verify → on failure diagnose + retry
(bounded) → on exhaustion roll back to the last good checkpoint and escalate.
Security/destructive units escalate immediately without spending attempts.

Pure orchestrator, mirroring ``agents/parallel_runner.py``: it owns the control
flow and takes injected hooks for the side effects, so it's provider-agnostic and
testable with fakes. The integration layer wires the real Checkpointer (Phase 2),
verify_unit (Phase 2), and RecoveryManager (existing) into these hooks.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Outcome statuses.
SUCCESS = "success"
ESCALATED = "escalated"  # retries exhausted, rolled back, hand to human/gate
ESCALATED_IMMEDIATE = "escalated_immediate"  # security/destructive — no attempts


@dataclass
class SelfHealOutcome:
    """Result of running one unit through the self-heal loop."""

    status: str
    attempts: int = 0
    rolled_back: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS


# Hook type aliases. attempt/verify/diagnose are async; snapshot/rollback are sync
# (matching the real Checkpointer). verify returns any object with a truthy
# ``passed`` attribute (e.g. VerifyResult from Phase 2).
AttemptFn = Callable[[int], Awaitable[Any]]
VerifyFn = Callable[[], Awaitable[Any]]
DiagnoseFn = Callable[[int], Awaitable[None]]


async def run_with_self_heal(
    *,
    label: str,
    attempt: AttemptFn,
    verify: VerifyFn,
    snapshot: Callable[[str], Any] | None = None,
    rollback: Callable[[Any], bool] | None = None,
    diagnose: DiagnoseFn | None = None,
    escalate_immediately: Callable[[], bool] | None = None,
    max_attempts: int = 3,
) -> SelfHealOutcome:
    """Run one unit with bounded retries + checkpoint rollback on exhaustion.

    Args:
        label: Human label for the unit (used in the checkpoint + events).
        attempt: ``async (attempt_number) -> Any`` — performs the unit once.
            May raise; an exception is treated as a failed attempt.
        verify: ``async () -> result`` — result.passed decides success.
        snapshot: ``(label) -> checkpoint`` taken before the first attempt.
        rollback: ``(checkpoint) -> bool`` run once, after retries are exhausted.
        diagnose: ``async (attempt_number) -> None`` between failed attempts
            (e.g. record recovery hints for the next attempt).
        escalate_immediately: ``() -> bool`` — if True, skip attempts entirely
            (security/destructive unit) and escalate.
        max_attempts: Maximum attempts before rollback + escalation (>= 1).

    Returns:
        SelfHealOutcome with status, attempt count, rollback flag, and an event
        log for the build report (#397).
    """
    max_attempts = max(1, max_attempts)
    events: list[dict[str, Any]] = []

    if escalate_immediately and escalate_immediately():
        events.append({"event": "escalate_immediate", "label": label})
        logger.info("[self-heal] %s escalated immediately (security/destructive)", label)
        return SelfHealOutcome(ESCALATED_IMMEDIATE, attempts=0, events=events)

    checkpoint = snapshot(label) if snapshot else None
    if checkpoint is not None:
        events.append({"event": "checkpoint", "label": label})

    for n in range(1, max_attempts + 1):
        try:
            await attempt(n)
        except Exception as exc:  # noqa: BLE001 - a crash is a failed attempt
            events.append({"event": "attempt_error", "attempt": n, "error": str(exc)})
            logger.warning("[self-heal] %s attempt %d raised: %s", label, n, exc)
        else:
            result = await verify()
            if getattr(result, "passed", False):
                events.append({"event": "verified", "attempt": n})
                logger.info("[self-heal] %s passed on attempt %d", label, n)
                return SelfHealOutcome(SUCCESS, attempts=n, events=events)
            failures = list(getattr(result, "failures", []) or [])
            events.append({"event": "verify_failed", "attempt": n, "failures": failures})
            logger.info("[self-heal] %s verify failed on attempt %d: %s", label, n, failures)

        # Diagnose before the next attempt (not after the last one).
        if diagnose and n < max_attempts:
            try:
                await diagnose(n)
            except Exception as exc:  # noqa: BLE001 - diagnosis is best-effort
                logger.debug("[self-heal] diagnose failed: %s", exc)

    rolled_back = False
    if checkpoint is not None and rollback:
        rolled_back = bool(rollback(checkpoint))
        events.append({"event": "rollback", "label": label, "ok": rolled_back})
    events.append({"event": "escalate", "label": label, "attempts": max_attempts})
    logger.info("[self-heal] %s exhausted %d attempts → escalate (rolled_back=%s)",
                label, max_attempts, rolled_back)
    return SelfHealOutcome(
        ESCALATED, attempts=max_attempts, rolled_back=rolled_back, events=events
    )
