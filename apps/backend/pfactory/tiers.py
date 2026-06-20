"""Difficulty-tier classifier for RFC-0011 label-driven intake.

Pure, dependency-free. Given the labels on an issue/work-item, decide the
difficulty *tier* that drives model / planning / human-gate / verification /
merge policy across the fleet (see RFC-0011 routing table).

Three tiers, highest-wins precedence::

    factory:hard  >  factory:medium  >  factory:low

A rewrite (``change_mode == "migration"``) always **forces hard**, regardless
of the labels present, because equivalence verification is mandatory for
rewrites (RFC-0010).

The classifier is tolerant of *scoped* label spellings — both ``factory:low``
and ``factory::low`` (GitLab scoped-label syntax) are recognised — so the same
canonical tier survives a round-trip through any tracker.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "Tier",
    "classify_tier",
    "tier_for",
    "TIER_LABEL_PREFIX",
]

TIER_LABEL_PREFIX = "factory:"

# Migration / rewrite change-mode that forces the hard tier.
_MIGRATION = "migration"


class Tier(str, Enum):
    """The three RFC-0011 difficulty tiers (ordered low < medium < hard)."""

    LOW = "low"
    MEDIUM = "medium"
    HARD = "hard"


# Precedence rank — highest wins.
_RANK: dict[Tier, int] = {Tier.LOW: 1, Tier.MEDIUM: 2, Tier.HARD: 3}


def _normalize(labels: object) -> list[str]:
    """Coerce labels (plain strings or ``{"name": ...}`` dicts) to a clean list.

    Tolerant by design: ``None``, non-iterables, blank names, and unexpected
    shapes are dropped rather than raising — intake must never crash on a
    malformed label set.
    """
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for lbl in labels:
        name = lbl.get("name", "") if isinstance(lbl, dict) else lbl
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name:
            out.append(name)
    return out


def _tier_from_label(name: str) -> Tier | None:
    """Map a single label name to a ``Tier`` if it is a tier label, else ``None``.

    Accepts both ``factory:low`` and the GitLab scoped form ``factory::low``.
    """
    low = name.lower()
    # Collapse a scoped-label double colon to the canonical single colon.
    low = low.replace("::", ":")
    if not low.startswith(TIER_LABEL_PREFIX):
        return None
    value = low[len(TIER_LABEL_PREFIX) :].strip()
    try:
        return Tier(value)
    except ValueError:
        return None


def classify_tier(labels: object) -> Tier | None:
    """Return the highest-precedence tier among ``labels``, or ``None``.

    ``None`` means no ``factory:<tier>`` label was present (the caller decides
    a default). Highest wins: ``hard`` beats ``medium`` beats ``low``.
    """
    best: Tier | None = None
    for name in _normalize(labels):
        tier = _tier_from_label(name)
        if tier is None:
            continue
        if best is None or _RANK[tier] > _RANK[best]:
            best = tier
    return best


def tier_for(classification: object, change_mode: str | None = None) -> Tier:
    """Resolve the effective tier for a classification, applying overrides.

    Reads the ``tier`` attribute off a ``Classification`` (or any object
    exposing one); falls back to ``Tier.MEDIUM`` when unset so work is never
    routed below a safe default. A ``change_mode == "migration"`` (rewrite)
    always forces ``Tier.HARD``.
    """
    tier = getattr(classification, "tier", None)
    if not isinstance(tier, Tier):
        tier = Tier.MEDIUM
    if isinstance(change_mode, str) and change_mode.strip().lower() == _MIGRATION:
        return Tier.HARD
    return tier
