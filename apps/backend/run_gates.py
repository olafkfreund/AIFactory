"""Run-start gate snapshot (#1983).

Every gate and model/thinking lookup of a run reads this one frozen object,
taken before the first agent session: the spec dirs are agent-writable after it.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path

from phase_config import (
    Phase,
    TaskMetadataConfig,
    get_phase_model,
    get_phase_thinking_budget,
    load_task_metadata,
)
from review.state import ReviewState, requires_review_before_coding
from solo_mode import is_solo_mode_enabled_for_spec


def _approved_plan_snapshot(
    spec_dir: Path, require_review: bool
) -> dict[str, object] | None:
    """Parse the plan once at run start when review gates coding (#1940).

    None when review is not required (callers seed from the live plan); ``{}``
    when the plan is missing/unreadable, which grants nothing.
    """
    if not require_review:
        return None
    try:
        plan = json.loads((spec_dir / "implementation_plan.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return plan if isinstance(plan, dict) else {}


@dataclass(frozen=True)
class RunGates:
    solo: bool
    requires_review: bool
    review: ReviewState
    approved_plan: dict[str, object] | None
    metadata: TaskMetadataConfig
    cli_model: str | None

    @classmethod
    def capture(
        cls, spec_dir: Path, source_spec_dir: Path | None, cli_model: str | None
    ) -> RunGates:
        gate_dir = source_spec_dir or spec_dir
        solo = is_solo_mode_enabled_for_spec(gate_dir)
        requires_review = bool(requires_review_before_coding(gate_dir))
        metadata: TaskMetadataConfig = copy.deepcopy(
            load_task_metadata(gate_dir) or TaskMetadataConfig()
        )
        return cls(
            solo=solo,
            requires_review=requires_review,
            review=ReviewState.load(gate_dir),
            approved_plan=_approved_plan_snapshot(
                spec_dir, not solo and requires_review
            ),
            metadata=metadata,
            cli_model=cli_model,
        )

    def approval_valid(self, spec_dir: Path) -> bool:
        """Stored hashes from the snapshot, live plan hash."""
        return not self.requires_review or self.review.is_approval_valid(spec_dir)

    def model(self, phase: Phase) -> str:
        # Lazy per phase: a registry `deny` must not raise for unused phases.
        model: str = get_phase_model(
            Path(os.devnull), phase, self.cli_model, metadata=self.metadata
        )
        return model

    def thinking_budget(self, phase: Phase) -> int | None:
        budget: int | None = get_phase_thinking_budget(
            Path(os.devnull), phase, metadata=self.metadata
        )
        return budget

    def subtask_model(self, subtask_id: object, live_model: object) -> str | None:
        if self.approved_plan is None:
            return live_model if isinstance(live_model, str) and live_model else None

        phases = self.approved_plan.get("phases")
        if isinstance(phases, list):
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                subtasks = phase.get("subtasks")
                if not isinstance(subtasks, list):
                    continue
                for subtask in subtasks:
                    if isinstance(subtask, dict) and subtask.get("id") == subtask_id:
                        model = subtask.get("model")
                        return model if isinstance(model, str) and model else None
        return None
