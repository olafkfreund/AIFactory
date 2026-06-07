"""
Review Tier Classifier — Tiered Human Review (Phase 4 of the self-healing
pipeline; tracking #397)
========================================================================

Routes a plan to the right human-review tier instead of one blocking gate for
everything:

* ``AUTO``     — proceed without a human gate (trusted #390-signed plans, solo,
                 or trivial work).
* ``ASYNC``    — async artifact review: the agent keeps building; the human
                 comments on artifacts and feedback is folded in between waves.
* ``BLOCKING`` — a hard gate before coding (high-risk paths or low planner
                 confidence).

Independently of the plan-time tier, any plan touching high-risk paths
(auth/secrets/migrations/infra/CI) also sets ``pre_merge_gate`` so a human signs
off before merge — defense in depth that composes with the Phase 5
security-reviewer.

Named ``review_tier`` (not ``risk_classifier``) to avoid colliding with the
existing ``analysis.risk_classifier`` spec-complexity system. Pure function over
the plan dict + a few signals; heavily unit-tested. The ReviewState wiring is the
integration step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Tiers.
AUTO = "auto"
ASYNC = "async"
BLOCKING = "blocking"

# Paths whose modification warrants extra human scrutiny. Matched
# case-insensitively as substrings of each subtask's declared files.
HIGH_RISK_PATTERNS: tuple[str, ...] = (
    "auth",
    "login",
    "session",
    "password",
    "secret",
    "credential",
    "token",
    "oauth",
    "iam",
    "rbac",
    "permission",
    "migration",  # also matches "migrations"
    "alembic",
    "schema.prisma",
    "terraform",
    ".tf",
    "dockerfile",
    "docker-compose",
    "k8s",
    "kubernetes",
    "helm",
    ".github/workflows",
    "ci.yml",
    "deploy",
    "payment",
    "billing",
)

# Default thresholds. Trivial = small change set; low confidence forces a gate.
TRIVIAL_MAX_SUBTASKS = 2
TRIVIAL_MAX_FILES = 3
LOW_CONFIDENCE_THRESHOLD = 0.5

_RISK_RE = re.compile("|".join(re.escape(p) for p in HIGH_RISK_PATTERNS), re.IGNORECASE)


@dataclass
class ReviewAssessment:
    """The review routing decision for a plan."""

    tier: str
    pre_merge_gate: bool = False
    reasons: list[str] = field(default_factory=list)
    high_risk_files: list[str] = field(default_factory=list)


def _all_files(plan: dict) -> list[str]:
    files: list[str] = []
    for phase in plan.get("phases", []) or []:
        if not isinstance(phase, dict):
            continue
        for st in phase.get("subtasks", []) or []:
            if not isinstance(st, dict):
                continue
            files.extend(st.get("files_to_create", []) or [])
            files.extend(st.get("files_to_modify", []) or [])
    return files


def _count_subtasks(plan: dict) -> int:
    return sum(
        len(p.get("subtasks", []) or [])
        for p in (plan.get("phases", []) or [])
        if isinstance(p, dict)
    )


def classify_review_tier(
    plan: dict,
    *,
    trusted: bool = False,
    solo: bool = False,
    confidence: float | None = None,
) -> ReviewAssessment:
    """Decide the review tier for ``plan``.

    Args:
        plan: implementation_plan.json dict (phases → subtasks with file sets).
        trusted: a verified #390 trusted plan (its signature is the gate).
        solo: solo-mode build (streamlined, owner-driven).
        confidence: planner self-reported confidence in [0,1], if available.

    Returns:
        ReviewAssessment with the plan-time tier, a pre_merge_gate flag, and
        human-readable reasons.
    """
    reasons: list[str] = []
    files = _all_files(plan)
    high_risk_files = sorted({f for f in files if _RISK_RE.search(f)})
    high_risk = bool(high_risk_files)
    low_confidence = confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD

    # High-risk paths always require a human before merge, regardless of tier.
    pre_merge_gate = high_risk
    if high_risk:
        reasons.append(f"touches high-risk paths: {', '.join(high_risk_files[:5])}")

    # Trusted (#390) → auto at plan time; the signature is the authority. A
    # high-risk trusted plan still gets the pre-merge gate above.
    if trusted:
        reasons.append("trusted (#390 signed plan)")
        return ReviewAssessment(AUTO, pre_merge_gate, reasons, high_risk_files)

    # High-risk or low-confidence → blocking gate before coding.
    if high_risk or low_confidence:
        if low_confidence:
            reasons.append(f"planner confidence {confidence} < {LOW_CONFIDENCE_THRESHOLD}")
        return ReviewAssessment(BLOCKING, pre_merge_gate, reasons, high_risk_files)

    # Solo or trivial → auto.
    n_subtasks = _count_subtasks(plan)
    if solo:
        reasons.append("solo mode")
        return ReviewAssessment(AUTO, pre_merge_gate, reasons, high_risk_files)
    if n_subtasks <= TRIVIAL_MAX_SUBTASKS and len(files) <= TRIVIAL_MAX_FILES:
        reasons.append(f"trivial ({n_subtasks} subtasks, {len(files)} files)")
        return ReviewAssessment(AUTO, pre_merge_gate, reasons, high_risk_files)

    # Everything else → async artifact review.
    reasons.append("standard change → async artifact review")
    return ReviewAssessment(ASYNC, pre_merge_gate, reasons, high_risk_files)
