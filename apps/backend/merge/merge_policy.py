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

RFC-0013 deployment overlay (#645): once the per-tier disposition is computed
the deployment policy may only ever make it *stricter*, never looser. A change
whose ``deployment`` block carries a ``high`` risk class — or that reaches
production — can NEVER auto-merge while its blocking system gates (``human-approval``,
and the required pre-deploy scans surfaced as CI checks) are unsatisfied, even at
``factory:low``. Production is VAL-4 and never autonomous; the deploy that *does*
run is always held behind a human. The overlay degrades, never fabricates:
absent / empty deployment inputs leave the RFC-0011 decision untouched, and
UNKNOWN delivery health (DORA ``available=false``) never relaxes a gate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = [
    "AUTO_MERGE",
    "HOLD_ASYNC",
    "HOLD_BLOCKING",
    "decide_merge",
    "deployment_block_reasons",
]

AUTO_MERGE = "auto-merge"
HOLD_ASYNC = "hold-async"
HOLD_BLOCKING = "hold-blocking"

# System gates that, when required by the deployment policy, must be cleared by a
# human (they cannot be satisfied autonomously). A required-but-unsatisfied gate
# in this set forces hold-blocking regardless of tier.
_BLOCKING_GATES: frozenset[str] = frozenset({"human-approval"})

# Deployment risk classes that may never auto-merge autonomously.
_NON_AUTONOMOUS_RISK: frozenset[str] = frozenset({"high"})

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


def _str_set(values: object) -> set[str]:
    """Normalize an iterable of gate/scan names to a lowercased str set.

    Tolerant of None and non-iterables (returns an empty set) so a malformed
    deployment block can never crash the decision — it just contributes no
    constraints (and therefore cannot *relax* anything).
    """
    if not isinstance(values, Iterable) or isinstance(values, str | bytes):
        return set()
    return {str(v).strip().lower() for v in values if str(v).strip()}


def deployment_block_reasons(
    deployment: Mapping[str, object] | None,
    *,
    satisfied_gates: object = None,
) -> list[str]:
    """Return the reasons (if any) the deployment policy forbids auto-merge.

    An empty list means the deployment block imposes no auto-merge block (the
    RFC-0011 tier decision stands). A non-empty list means the change must be
    held for a blocking (human) approval regardless of tier — each entry is a
    human-readable reason for the audit trail / PR comment.

    Honest + conservative (RFC-0013 §3/§6):
      * ``risk_class: high`` => never autonomous.
      * ``production_classification: production`` => never autonomous (VAL-4).
      * any required ``system_gates`` in ``_BLOCKING_GATES`` that is NOT in
        ``satisfied_gates`` => held until a human clears it.
      * UNKNOWN delivery health never *relaxes* a gate — it is simply not a
        reason to merge, so it is intentionally not consulted here.
    """
    if not isinstance(deployment, Mapping):
        return []

    reasons: list[str] = []

    risk_class = str(deployment.get("risk_class", "")).strip().lower()
    if risk_class in _NON_AUTONOMOUS_RISK:
        reasons.append(f"deployment risk_class={risk_class} is never auto-merged")

    prod_class = str(deployment.get("production_classification", "")).strip().lower()
    if prod_class == "production":
        reasons.append("production change is VAL-4 / never autonomous (human-approval required)")

    required_gates = _str_set(deployment.get("system_gates"))
    have = _str_set(satisfied_gates)
    for gate in sorted(required_gates & _BLOCKING_GATES):
        if gate not in have:
            reasons.append(f"required system gate '{gate}' is not satisfied")

    return reasons


def decide_merge(
    tier: str,
    *,
    host_ci_green: bool,
    tfactory_verdict: object,
    achieved_val: object,
    val_floor: object,
    ci_parity: bool,
    deployment: Mapping[str, object] | None = None,
    satisfied_gates: object = None,
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
        deployment: the optional RFC-0013 ``deployment`` contract block. When
            present it may only ever make the decision STRICTER (never merge a
            high-risk/production change without its blocking gates). Absent or
            empty => the RFC-0011 decision is returned unchanged (back-compat).
        satisfied_gates: the named system gates already cleared (e.g. an
            ``human-approval`` recorded on the PR). Used only to decide whether a
            required blocking gate is satisfied.

    Returns:
        One of ``AUTO_MERGE`` / ``HOLD_ASYNC`` / ``HOLD_BLOCKING``.
    """
    bucket = _TIER_ALIASES.get((tier or "").strip().lower())
    if bucket is None:
        # Unknown tier => safest non-merging disposition.
        return HOLD_BLOCKING

    # RFC-0013 deployment overlay: a high-risk / production change with an
    # unsatisfied blocking gate is held for blocking approval BEFORE the tier
    # is even consulted. The overlay can only tighten, never loosen.
    if deployment_block_reasons(deployment, satisfied_gates=satisfied_gates):
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
