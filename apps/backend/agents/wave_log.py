"""
Parallel Wave Event Persistence (#393)
======================================

The parallel executor's progress markers (``[parallel] Wave N: …``) were written
to stdout only, so there was no after-the-fact way to confirm whether the wave
path engaged — making the #376 benchmark impossible to verify.

``WaveRecorder`` persists wave lifecycle events to two greppable artifacts in the
spec directory:

* ``parallel_report.json`` — structured record: waves, subtasks per wave,
  observed concurrency, per-wave wall-clock, and the final phase outcome. The
  benchmark and the UI read this.
* ``build-progress.txt`` — human-readable ``[parallel] …`` lines appended next to
  the existing ``[x] subtask`` lines, so a parallel build is visibly distinct
  from a serial one in the persisted log.

A serial build writes neither parallel artifact, so the two are trivially
distinguishable (acceptance criterion 2). This module is pure side-effect /
observability — it never changes wave scheduling.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPORT_FILENAME = "parallel_report.json"
PROGRESS_FILENAME = "build-progress.txt"


def _default_clock() -> float:
    import time

    return time.monotonic()


def _default_now() -> str:
    return datetime.now().isoformat()


class WaveRecorder:
    """Accumulates wave events and persists them to the spec directory.

    Args:
        spec_dir: Spec directory the build runs against.
        workers_max: Configured max concurrent subtasks per wave.
        phase_name: Name of the phase being run in waves.
        source_spec_dir: Optional main-project spec dir to mirror artifacts into
            (worktree builds write to the child spec; the UI reads the main one).
        clock: Monotonic time source (injectable for tests).
        now: Wall-clock ISO timestamp source (injectable for tests).
    """

    def __init__(
        self,
        spec_dir: Path,
        *,
        workers_max: int,
        phase_name: str = "",
        source_spec_dir: Path | None = None,
        clock: Callable[[], float] = _default_clock,
        now: Callable[[], str] = _default_now,
    ) -> None:
        self.spec_dir = Path(spec_dir)
        self.source_spec_dir = Path(source_spec_dir) if source_spec_dir else None
        self.workers_max = workers_max
        self.phase_name = phase_name
        self._clock = clock
        self._now = now
        self._waves: list[dict[str, Any]] = []
        self._pending: dict[str, Any] | None = None
        self._summary: dict[str, Any] = {}

    # -- wave lifecycle ----------------------------------------------------

    def record_wave(self, wave_num: int, subtask_ids: list[str]) -> None:
        """Record the start of a wave (called from the on_wave callback)."""
        self._close_pending()
        concurrency = min(len(subtask_ids), self.workers_max)
        self._pending = {
            "wave": wave_num,
            "subtask_ids": list(subtask_ids),
            "concurrency": concurrency,
            "started_at": self._now(),
            "_start_mono": self._clock(),
        }
        ids = ", ".join(subtask_ids)
        self._append_progress(
            f"[parallel] Wave {wave_num}: {len(subtask_ids)} subtask(s) "
            f"— {ids} (concurrency={concurrency})"
        )
        self._write_report()

    def finish(self, result: Any = None) -> dict[str, Any]:
        """Finalize the phase: close the last wave and persist the summary.

        ``result`` is the :class:`PhaseRunResult` (duck-typed: optional
        completed_ids/failed_ids/remaining_ids/stalled/waves attributes).
        Returns the written report dict.
        """
        self._close_pending()
        if result is not None:
            self._summary = {
                "completed": list(getattr(result, "completed_ids", []) or []),
                "failed": list(getattr(result, "failed_ids", []) or []),
                "remaining": list(getattr(result, "remaining_ids", []) or []),
                "stalled": bool(getattr(result, "stalled", False)),
            }
            peak = self.observed_max_concurrency
            self._append_progress(
                f"[parallel] Phase {self.phase_name or '?'!s} complete: "
                f"{len(self._waves)} wave(s), "
                f"{len(self._summary['completed'])} completed, peak concurrency {peak}"
            )
        self._write_report()
        return self._build_report()

    # -- derived -----------------------------------------------------------

    @property
    def observed_max_concurrency(self) -> int:
        return max((w["concurrency"] for w in self._waves), default=0)

    def _close_pending(self) -> None:
        if self._pending is None:
            return
        pending = self._pending
        start = pending.pop("_start_mono")
        pending["duration_s"] = round(self._clock() - start, 3)
        self._waves.append(pending)
        self._pending = None

    # -- persistence -------------------------------------------------------

    def _build_report(self) -> dict[str, Any]:
        report = {
            "spec": self.spec_dir.name,
            "parallel": True,
            "workers_max": self.workers_max,
            "phase": self.phase_name,
            "total_waves": len(self._waves) + (1 if self._pending else 0),
            "observed_max_concurrency": self.observed_max_concurrency,
            "waves": list(self._waves),
            "updated_at": self._now(),
        }
        if self._pending:
            live = {k: v for k, v in self._pending.items() if k != "_start_mono"}
            report["waves"] = report["waves"] + [live]
        report.update(self._summary)
        return report

    def _write_report(self) -> None:
        report = self._build_report()
        payload = json.dumps(report, indent=2)
        for target in self._report_paths():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload, encoding="utf-8")
            except OSError as exc:  # never break a build on observability I/O
                logger.debug("wave report write failed (%s): %s", target, exc)

    def _append_progress(self, line: str) -> None:
        for base in self._spec_dirs():
            progress = base / PROGRESS_FILENAME
            try:
                with open(progress, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                logger.debug("build-progress append failed (%s): %s", progress, exc)

    def _spec_dirs(self) -> list[Path]:
        dirs = [self.spec_dir]
        if self.source_spec_dir and self.source_spec_dir != self.spec_dir:
            dirs.append(self.source_spec_dir)
        return dirs

    def _report_paths(self) -> list[Path]:
        return [d / REPORT_FILENAME for d in self._spec_dirs()]


def load_parallel_report(spec_dir: Path) -> dict[str, Any] | None:
    """Read a persisted ``parallel_report.json`` (None if absent/invalid)."""
    path = Path(spec_dir) / REPORT_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
