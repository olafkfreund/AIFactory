"""
Per-Unit Verifier (Phase 2 of the self-healing pipeline; tracking #397)
======================================================================

A Claude-Code-style "hook" that runs the project's test/lint/type gates right
after a subtask or wave, turning a silent regression into a structured signal the
self-heal loop (#397 Phase 3) acts on: pass → continue; fail → diagnose, retry,
or roll back.

Thin wrapper over ``agents/gate_runner.py`` (#376) — it owns gate detection and
execution; this module just runs them per-unit and shapes the verdict. The
``runner`` hook is injectable so tests never spawn real subprocesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .gate_runner import (
    Gate,
    GateResult,
    detect_gates,
    failing_gates,
    run_gates,
    summarize_gates,
)


@dataclass
class VerifyResult:
    """Outcome of verifying one unit of work."""

    passed: bool
    summary: str = ""
    results: list[GateResult] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        """True if any gate actually executed (vs none detected)."""
        return bool(self.results)


async def verify_unit(
    project_dir: Path,
    *,
    gates: list[Gate] | None = None,
    runner: Callable[[list[str], Path], tuple[int | None, str]] | None = None,
) -> VerifyResult:
    """Run the project's gates over ``project_dir`` and shape the verdict.

    No gates detected → ``passed=True`` (nothing to verify is not a failure).
    A missing tool is ``skipped`` (not a failure). Only real non-zero exits fail.
    """
    results = await run_gates(project_dir, gates, runner=runner)
    fails = failing_gates(results)
    return VerifyResult(
        passed=not fails,
        summary=summarize_gates(results),
        results=results,
        failures=[r.name for r in fails],
    )


def verifier_gates(project_dir: Path) -> list[Gate]:
    """Convenience: the gates that would run for this project (for logging/plan)."""
    return detect_gates(project_dir)
