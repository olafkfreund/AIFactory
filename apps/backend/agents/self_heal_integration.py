"""Self-healing integration layer (issue #415).

The self-healing *modules* from #397 are pure and unit-tested
(``self_heal``, ``checkpoint``, ``verifier``, ``review_tier``,
``security_reviewer``, ``artifacts``). This module is the thin, **default-off**
wiring that the live executor calls so enabling self-healing is a single flag
and the existing build loop is untouched when it is off.

Flag: ``AIFACTORY_SELF_HEAL`` (1/true/yes/on enables; anything else, or unset,
keeps the loop exactly as it was).

Everything here is additive and side-effect-light: a disabled flag makes every
entrypoint a no-op (returns ``None``/passthrough), so wiring these into the
executor changes nothing until an operator opts in for a validation run.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .checkpoint import Checkpointer
from .security_reviewer import GateDecision, gate_decision, review_diff, scan_diff_static
from .verifier import verify_unit

_TRUTHY = {"1", "true", "yes", "on"}


def is_self_heal_enabled(env: dict | None = None) -> bool:
    """True when AIFACTORY_SELF_HEAL is set to a truthy value (default off)."""
    env = env if env is not None else os.environ
    return str(env.get("AIFACTORY_SELF_HEAL", "")).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Item 1 — self-heal wrapper around one subtask attempt
# ---------------------------------------------------------------------------

async def self_heal_subtask(
    *,
    label: str,
    attempt: Callable[[int], Awaitable[Any]],
    project_dir: Path,
    checkpointer: Checkpointer | None = None,
    escalate_immediately: Callable[[], bool] | None = None,
    max_attempts: int = 2,
    verify: Callable[[], Awaitable[Any]] | None = None,
):
    """Run one subtask ``attempt`` with verify→retry→rollback→escalate.

    Returns a ``SelfHealOutcome`` when self-heal is enabled, else ``None`` (the
    caller then runs its normal path). ``verify`` defaults to the project's
    verification gates (``verify_unit``). Imported lazily so this module stays
    cheap to import when the flag is off.
    """
    if not is_self_heal_enabled():
        return None
    from .self_heal import run_with_self_heal

    cp = checkpointer or Checkpointer(project_dir)

    async def _verify():
        return await verify_unit(project_dir)

    return await run_with_self_heal(
        label=label,
        attempt=attempt,
        verify=verify or _verify,
        snapshot=cp.snapshot,
        rollback=cp.rollback,
        escalate_immediately=escalate_immediately,
        max_attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# Item 2 — review-tier assessment (recorded; gates only when enabled)
# ---------------------------------------------------------------------------

def assess_review_tier(
    plan: dict, *, trusted: bool = False, solo: bool = False,
    confidence: float | None = None,
):
    """Classify the plan's review tier (always safe to call). Returns the
    ReviewAssessment, or None if classification fails."""
    try:
        from review_tier import classify_review_tier

        return classify_review_tier(
            plan, trusted=trusted, solo=solo, confidence=confidence
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Item 3 — security review before merge
# ---------------------------------------------------------------------------

async def security_pre_merge_gate(
    diff_text: str,
    *,
    project_dir: Path | None = None,
    threshold: str = "high",
    llm_scan: Callable[[str], Any] | None = None,
) -> GateDecision | None:
    """Scan a pre-merge diff and decide whether to block. No-op (None) when the
    flag is off or the diff is empty. Static scan always runs; an optional
    ``llm_scan`` subagent augments it. Never raises into the merge path."""
    if not is_self_heal_enabled() or not diff_text:
        return None
    try:
        report = await review_diff(
            diff_text, project_dir=project_dir, threshold=threshold, llm_scan=llm_scan
        )
        return report.decision
    except Exception:
        # A scanner failure must not block a merge that was otherwise fine.
        return gate_decision([], threshold=threshold)


def security_pre_merge_gate_sync(
    diff_text: str, *, threshold: str = "high"
) -> GateDecision | None:
    """Synchronous static-only security gate for sync call sites (the worktree
    merge path). No-op (None) when the flag is off or the diff is empty; never
    raises into the merge."""
    if not is_self_heal_enabled() or not diff_text:
        return None
    try:
        return gate_decision(scan_diff_static(diff_text), threshold=threshold)
    except Exception:
        return gate_decision([], threshold=threshold)


# ---------------------------------------------------------------------------
# Item 4 — artifact emission
# ---------------------------------------------------------------------------

def emit_plan_artifact(
    spec_dir: Path, plan: dict, *, source_spec_dir: Path | None = None
):
    """Emit the plan as a build Artifact (no-op when the flag is off). Returns
    the Artifact or None. Best-effort; never raises into the build."""
    if not is_self_heal_enabled():
        return None
    try:
        from .artifacts import ArtifactWriter

        writer = ArtifactWriter(spec_dir, source_spec_dir=source_spec_dir)
        feature = plan.get("feature", spec_dir.name)
        lines = [f"# Plan — {feature}", ""]
        for ph in plan.get("phases", []):
            lines.append(f"## Phase {ph.get('phase')}: {ph.get('name', '')}")
            for st in ph.get("subtasks", []):
                lines.append(f"- `{st.get('id')}` {st.get('description', '')[:120]}")
            lines.append("")
        return writer.write("plan", f"Plan — {feature}", "\n".join(lines), name="plan")
    except Exception:
        return None
