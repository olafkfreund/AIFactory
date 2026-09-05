"""RFC-0011 / RFC-0009 merge-gate decision (#637).

Pure decision function: given a PR's tier (``review_tier`` / ``autonomy_tier``)
and its gate signals — host CI status, the TFactory verdict, and the RFC-0006
achieved Verification Assurance Level vs the tier's floor — decide whether the PR
may **auto-merge**, must **hold for async approval**, or must **hold for blocking
approval**.

This is intentionally NOT in ``merge/auto_merger/`` (a deterministic
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
production — can NEVER auto-merge while ANY of its required system gates
(``human-approval``, and the required pre-deploy scans surfaced as CI checks) are
unsatisfied, even at ``factory:low``. Production is VAL-4 and never autonomous;
the deploy that *does* run is always held behind a human. The overlay degrades,
never fabricates: absent / empty deployment inputs leave the RFC-0011 decision untouched, and
UNKNOWN delivery health (DORA ``available=false``) never relaxes a gate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

__all__ = [
    "AUTO_MERGE",
    "HOLD_ASYNC",
    "HOLD_BLOCKING",
    "decide_merge",
    "deployment_block_reasons",
    "floor_from_paths",
    "raise_review_tier",
    "tier_permits_auto_merge",
]

AUTO_MERGE = "auto-merge"
HOLD_ASYNC = "hold-async"
HOLD_BLOCKING = "hold-blocking"

# System gates that can only ever be cleared by a HUMAN. Every required gate
# blocks while it is unsatisfied; this set only changes the wording of the
# reason, so the audit trail says which ones no amount of automation can clear.
_HUMAN_ONLY_GATES: frozenset[str] = frozenset({"human-approval"})

# Back-compat alias for the old name (it was public-ish via the module).
_BLOCKING_GATES = _HUMAN_ONLY_GATES

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


# Ordinal for the canonical buckets, so a tier can be RAISED and never lowered
# (#1456). Ranking the bucket rather than the spelling means auto/low and
# blocking/hard compare correctly against each other.
_BUCKET_RANK: dict[str, int] = {"low": 0, "medium": 1, "hard": 2}


def floor_from_paths(changed: object) -> str:
    """The lowest review tier a change touching *changed* may be reviewed at.

    Path-derived, so a change cannot self-declare itself routine: any changed
    file matching ``review_tier.HIGH_RISK_PATTERNS`` (auth / secrets /
    migrations / infra / CI) floors it at ``blocking`` — the same verdict
    ``review_tier.classify_review_tier`` reaches for a high-risk plan. The
    pattern table is IMPORTED, never restated: a second copy of a shared rule
    drifts (Factory#590).

    Returns ``"auto"`` (no floor) for an empty or unmatched change set: a diff
    that matched no high-risk pattern genuinely carries no floor.

    Fails CLOSED, not open. If the pattern table itself cannot be imported the
    classifier did not run, and a classifier that did not run has not cleared
    anything — it returns ``"blocking"``. The previous ``"auto"`` was worse than
    it looks: ``"auto"`` is bucket rank 0, not the ``-1``
    :func:`raise_review_tier` gives an unrecognised value, so a vanished
    auth/secrets/migrations/infra/CI classifier could not raise ANY tier and the
    floor silently disappeared for every change.

    Lazy import: ``review_tier`` is a top-level module of the backend package,
    which is on ``sys.path`` only at runtime — the same reason ``pr_endgame``
    imports this module lazily.
    """
    try:
        from review_tier import (  # type: ignore[import-not-found,unused-ignore] # noqa: PLC0415
            _RISK_RE,
        )
    except ImportError:
        return "blocking"
    if not isinstance(changed, Iterable) or isinstance(changed, str | bytes):
        return "auto"
    for path in changed:
        if isinstance(path, str) and path.strip() and _RISK_RE.search(path):
            return "blocking"
    return "auto"


def raise_review_tier(tier: str | None, floor: str | None) -> str | None:
    """The stricter of *tier* and *floor*. NEVER lowers a tier.

    Accepts either spelling on either side (``auto``/``low`` …). An
    unrecognised or absent ``tier`` ranks below every real floor, so a floor
    still applies to it; an unrecognised ``floor`` can never lower a real tier.
    """
    t = _BUCKET_RANK.get(_TIER_ALIASES.get(str(tier or "").strip().lower(), ""), -1)
    f = _BUCKET_RANK.get(_TIER_ALIASES.get(str(floor or "").strip().lower(), ""), -1)
    return floor if f > t else tier


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

    An ABSENT floor (None / blank) means no floor was declared, so any achieved
    level satisfies it. An UNPARSEABLE floor ("VAL-three", "high") is a
    different thing entirely: it is a gate nobody can read, and a gate nobody
    can read is not a gate anyone may waive — it fails, matching the
    already-fail-closed branch above for a missing achieved level.
    """
    a = _val_rank(achieved_val)
    f = _val_rank(val_floor)
    if a is None:
        return False
    if f is None:
        return _floor_absent(val_floor)
    return a >= f


def _floor_absent(val_floor: object) -> bool:
    """Whether *val_floor* declares no floor at all (vs. one we cannot parse)."""
    if val_floor is None:
        return True
    return isinstance(val_floor, str) and not val_floor.strip()


def _val_rank(val: object) -> int | None:
    if isinstance(val, bool):  # guard: bool is an int subclass
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        # The FIRST integer run, never every digit concatenated: joining the
        # digits turned "VAL-1 of 3" into 13 and "VAL-2 (3 lanes)" into 23, so
        # any annotated level outranked every floor and the VAL gate inverted.
        match = re.search(r"\d+", val)
        if match:
            return int(match.group())
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
      * ANY required ``system_gates`` entry that is NOT in ``satisfied_gates``
        => held. This used to intersect the required set with
        ``_BLOCKING_GATES`` (``{"human-approval"}``), so a contract declaring
        ``system_gates: ["security-scan", "sbom", "dr-signoff"]`` produced ZERO
        reasons while the module docstring promised those pre-deploy scans held
        the merge. The docstring was right and the code was a near no-op; the
        code now matches it. A gate we cannot see evidence for is unsatisfied.
      * UNKNOWN delivery health never *relaxes* a gate — it is simply not a
        reason to merge, so it is intentionally not consulted here.

    ``satisfied_gates`` is what makes this satisfiable: a caller that never
    passes it can only ever see every declared gate as outstanding, which is why
    ``pr_endgame`` now reads the recorded approvals and passes them in.
    """
    if not isinstance(deployment, Mapping):
        return []

    reasons: list[str] = []

    risk_class = str(deployment.get("risk_class", "")).strip().lower()
    if risk_class in _NON_AUTONOMOUS_RISK:
        reasons.append(f"deployment risk_class={risk_class} is never auto-merged")

    prod_class = str(deployment.get("production_classification", "")).strip().lower()
    if prod_class == "production":
        reasons.append(
            "production change is VAL-4 / never autonomous (human-approval required)"
        )

    required_gates = _str_set(deployment.get("system_gates"))
    have = _str_set(satisfied_gates)
    for gate in sorted(required_gates - have):
        if gate in _HUMAN_ONLY_GATES:
            reasons.append(
                f"required system gate '{gate}' is not satisfied "
                "(only a human can clear it)"
            )
        else:
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


def tier_permits_auto_merge(tier: str | None) -> bool:
    """Whether this tier may auto-merge AT ALL, ignoring the gate signals.

    The tier's *ceiling*, not the decision. :func:`decide_merge` is the full
    decision and additionally requires host CI, the TFactory verdict, the VAL
    floor and CI parity before a ``low`` tier actually merges. This exists for
    the caller that has the tier but genuinely cannot supply those signals --
    ``pr_endgame`` knows the review verdict and nothing else -- so that it can
    NARROW its own gate without fabricating greens it has not measured.

    Use it to tighten an existing decision, never to widen one: True here means
    only "the tier does not forbid it", and the caller's own gates still have
    to pass.

    Absent / blank tier => True, so a task carrying no ``reviewTier`` behaves
    exactly as it did before -- the same back-compat rule the RFC-0013
    deployment overlay uses for absent inputs. An UNRECOGNISED tier => False,
    matching ``decide_merge``'s HOLD_BLOCKING for an unknown spelling: a tier
    nobody can read is not a licence to merge (#1158).
    """
    if tier is None or not str(tier).strip():
        return True
    return _TIER_ALIASES.get(str(tier).strip().lower()) == "low"
