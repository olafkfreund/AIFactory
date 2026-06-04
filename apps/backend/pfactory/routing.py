"""Map the PFactory taxonomy onto AIFactory scheduling + routing (epic #327, #331).

Two pure decisions, derived from a :class:`~pfactory.taxonomy.Classification`:

* **priority** — ``priority:p0..p3`` → a sort rank (p0 first) for the backlog.
* **routing** — where a governed child should go: TFactory for test generation
  (``handoff:tfactory`` or ``type:testing``) vs the AIFactory coder.

These are kept pure (no I/O) so callers — the task-list scheduler and the
task-start route — can apply them at their own seam and unit-test the decision
in isolation.
"""

from __future__ import annotations

from .taxonomy import Classification

__all__ = ["priority_rank", "routing_target", "CODER", "TFACTORY", "UNRANKED"]

CODER = "coder"
TFACTORY = "tfactory"

# p0 = critical path → scheduled first. Unknown / missing priority sorts last.
_PRIORITY_ORDER = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
UNRANKED = 99


def priority_rank(priority: str | None) -> int:
    """Sort rank for a PFactory ``priority:*`` value (e.g. ``"p0"``).

    Lower ranks first (p0 → 0 … p3 → 3); anything unknown/missing → ``UNRANKED``
    so it sorts after the explicitly-prioritised work.
    """
    if not isinstance(priority, str):
        return UNRANKED
    return _PRIORITY_ORDER.get(priority.strip().lower(), UNRANKED)


def routing_target(classification: Classification) -> str:
    """Decide where a (governed) child should go: ``TFACTORY`` or ``CODER``.

    An explicit ``handoff`` is authoritative: ``handoff:tfactory`` → TFactory,
    ``handoff:aifactory`` → the coder. With no handoff, a ``type:testing`` child
    is test-generation work → TFactory; everything else → the coder.
    """
    if classification.handoff == TFACTORY:
        return TFACTORY
    if classification.handoff == "aifactory":
        return CODER
    if "testing" in classification.types:
        return TFACTORY
    return CODER
