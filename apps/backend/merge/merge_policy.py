"""RFC-0011 / RFC-0009 merge-gate decision (#637).

Pure decision function: given a PR's tier (``review_tier`` / ``autonomy_tier``)
and its gate signals — host CI status, the TFactory verdict, and the RFC-0006
achieved Verification Assurance Level vs the tier's floor — decide whether the PR
may **auto-merge**, must **hold for async approval**, or must **hold for blocking
approval**.

This is intentionally NOT in ``merge/auto_merger.py`` (a deterministic
code-conflict merger). It carries no provider/IO coupling so the decision matrix
is exhaustively unit-testable.

Tier policy (RFC-0011 routing table):

    low    -> auto-merge ONLY when host CI is green AND the TFactory verdict
              passes AND achieved_val >= val_floor AND ci_parity; otherwise it
              degrades to hold-async (never merges below the floor).
    medium -> hold-async  (merge after async approval).
    hard   -> hold-blocking (blocking approval, then merge).
"""

from __future__ import annotations

__all__ = ["AUTO_MERGE", "HOLD_ASYNC", "HOLD_BLOCKING", "decide_merge"]

AUTO_MERGE = "auto-merge"
HOLD_ASYNC = "hold-async"
HOLD_BLOCKING = "hold-blocking"

# Map any tier spelling (autonomy_tier low/medium/hard OR the equivalent
# review_tier auto/async/blocking) onto the canonical low/medium/hard bucket.
_TIER_ALIASES: dict[str, str] = {
    "low": "low",
    "auto": "low",
    "medium": "medium",
    "async": "medium",
    "hard": "hard",
    "blocking": "hard",
}


def _verdict_pass(tfactory_verdict: object) -> bool:
    """Whether the TFactory verdict counts as a pass.

    Accepts a bool, or a string like ``"pass"`` / ``"passed"`` / ``"green"``.
    Anything else (None, "fail", "handback", …) is not a pass.
    """
    if isinstance(tfactory_verdict, bool):
        return tfactory_verdict
    if isinstance(tfactory_verdict, str):
        return tfactory_verdict.strip().lower() in {"pass", "passed", "green", "ok"}
    return False


def _val_meets_floor(achieved_val: object, val_floor: object) -> bool:
    """Whether the achieved VAL meets/exceeds the floor.

    VALs are ordinal (VAL-1 < VAL-2 < VAL-3). Accepts ints (1/2/3) or strings
    like ``"VAL-2"`` / ``"val2"`` / ``"2"``. A missing achieved level fails.
    """
    a = _val_rank(achieved_val)
    f = _val_rank(val_floor)
    if a is None:
        return False
    if f is None:
        return True  # no floor declared => any achieved level satisfies
    return a >= f


def _val_rank(val: object) -> int | None:
    if isinstance(val, bool):  # guard: bool is an int subclass
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        digits = "".join(ch for ch in val if ch.isdigit())
        if digits:
            return int(digits)
    return None


def decide_merge(
    tier: str,
    *,
    host_ci_green: bool,
    tfactory_verdict: object,
    achieved_val: object,
    val_floor: object,
    ci_parity: bool,
) -> str:
    """Decide the merge disposition for a PR.

    Args:
        tier: ``review_tier`` (auto|async|blocking) or ``autonomy_tier``
            (low|medium|hard) — either spelling is accepted.
        host_ci_green: the host provider's required CI checks are all green.
        tfactory_verdict: TFactory's verification verdict (bool or string).
        achieved_val: the RFC-0006 achieved Verification Assurance Level.
        val_floor: the tier's required VAL floor.
        ci_parity: the host CI ran the same checks TFactory verified (RFC-0009).

    Returns:
        One of ``AUTO_MERGE`` / ``HOLD_ASYNC`` / ``HOLD_BLOCKING``.
    """
    bucket = _TIER_ALIASES.get((tier or "").strip().lower())
    if bucket is None:
        # Unknown tier => safest non-merging disposition.
        return HOLD_BLOCKING

    if bucket == "hard":
        return HOLD_BLOCKING
    if bucket == "medium":
        return HOLD_ASYNC

    # low: auto-merge only when every gate is satisfied; else refuse to merge
    # and degrade to async review (never merge below floor / without parity).
    if (
        host_ci_green
        and _verdict_pass(tfactory_verdict)
        and _val_meets_floor(achieved_val, val_floor)
        and ci_parity
    ):
        return AUTO_MERGE
    return HOLD_ASYNC
