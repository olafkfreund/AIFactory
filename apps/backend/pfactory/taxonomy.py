"""PFactory tag-taxonomy (v1) classifier — the AIFactory side of the pickup
contract (epic #327, issue #329).

Pure, dependency-free label classification: given the labels on a GitHub issue
(or those stored in a spec's ``requirements.json``), decide whether it is a
*governed* PFactory spec routed to AIFactory, whether it is an epic, and surface
the descriptive taxonomy (type / plan-type / priority / sev) for downstream use
(#330, #331).

A "governed" spec carries the mandatory ``pfactory`` marker AND ``handoff:aifactory``:
PFactory already ran its architecture/security/best-practice/feasibility gates
and a human approved it, so AIFactory skips its own up-front planning/approval
gate for these (see ``is_governed_requirements`` + the agent service).

Full contract: guides/PFACTORY_TAG_TAXONOMY.md.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Classification",
    "classify_labels",
    "classify_requirements",
    "is_governed_requirements",
    "TAXONOMY_VERSION",
]

TAXONOMY_VERSION = "v1"

# Marker + routing labels — the only ones that gate ingestion behaviour.
MARKER = "pfactory"
HANDOFF_AIFACTORY = "handoff:aifactory"
HANDOFF_TFACTORY = "handoff:tfactory"
EPIC = "epic"

# Descriptive-taxonomy prefixes (consumed by #330/#331).
_TYPE_PREFIX = "type:"
_PLAN_TYPE_PREFIX = "plan-type:"
_PRIORITY_PREFIX = "priority:"
_SEV_PREFIX = "sev:"


@dataclass(frozen=True)
class Classification:
    """Result of classifying an issue's labels against the PFactory taxonomy."""

    is_pfactory: bool  # carries the mandatory `pfactory` marker
    handoff: str | None  # "aifactory" | "tfactory" | None
    is_epic: bool  # `epic` label present
    governed: bool  # pfactory AND routed to aifactory → skip planning gate
    types: tuple[str, ...]  # type:* values, prefix stripped
    plan_type: str | None  # first plan-type:* value, prefix stripped
    priority: str | None  # first priority:* value (e.g. "p1"), prefix stripped
    sev: str | None  # first sev:* value, prefix stripped


def _normalize(labels: object) -> list[str]:
    """Coerce labels (plain strings or ``{"name": ...}`` dicts) to a clean list.

    Tolerant by design: ``None``, non-iterables, blank names, and unexpected
    shapes are dropped rather than raising — pickup must never crash on a
    malformed label set.
    """
    # Only genuine sequences are label lists; a bare string/dict/number/None is
    # treated as "no labels" rather than raising — pickup must never crash.
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for lbl in labels:
        if isinstance(lbl, dict):
            name = lbl.get("name", "")
        else:
            name = lbl
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name:
            out.append(name)
    return out


def classify_labels(labels: object) -> Classification:
    """Classify a list of label names (or ``{"name": ...}`` dicts) — taxonomy v1."""
    names = _normalize(labels)
    lower = {n.lower() for n in names}

    if HANDOFF_AIFACTORY in lower:
        handoff: str | None = "aifactory"
    elif HANDOFF_TFACTORY in lower:
        handoff = "tfactory"
    else:
        handoff = None

    is_pfactory = MARKER in lower

    def _first(prefix: str) -> str | None:
        for n in names:
            if n.lower().startswith(prefix):
                return n[len(prefix) :]
        return None

    types = tuple(
        n[len(_TYPE_PREFIX) :] for n in names if n.lower().startswith(_TYPE_PREFIX)
    )

    return Classification(
        is_pfactory=is_pfactory,
        handoff=handoff,
        is_epic=EPIC in lower,
        governed=is_pfactory and handoff == "aifactory",
        types=types,
        plan_type=_first(_PLAN_TYPE_PREFIX),
        priority=_first(_PRIORITY_PREFIX),
        sev=_first(_SEV_PREFIX),
    )


def classify_requirements(requirements: object) -> Classification:
    """Classify from a ``requirements.json`` dict.

    Labels are read from ``githubIssue.labels`` (the GitHub import path) and,
    when absent, from ``metadata.labels`` (the PFactory-direct-write path) —
    either path can trigger pickup (#329).
    """
    if not isinstance(requirements, dict):
        return classify_labels([])
    gh = requirements.get("githubIssue")
    labels = gh.get("labels") if isinstance(gh, dict) else None
    if not labels:
        meta = requirements.get("metadata")
        labels = meta.get("labels") if isinstance(meta, dict) else None
    return classify_labels(labels)


def is_governed_requirements(requirements: object) -> bool:
    """Whether a spec's ``requirements.json`` is a governed PFactory spec.

    Prefers an explicit persisted flag (``governed`` or ``pfactory.governed``,
    written at import time), falling back to live label classification so the
    PFactory-direct-write path is also recognised.
    """
    if not isinstance(requirements, dict):
        return False
    if isinstance(requirements.get("governed"), bool):
        return requirements["governed"]
    pf = requirements.get("pfactory")
    if isinstance(pf, dict) and isinstance(pf.get("governed"), bool):
        return pf["governed"]
    return classify_requirements(requirements).governed
