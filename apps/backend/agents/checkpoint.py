"""
Build Checkpoints (Phase 2 of the self-healing pipeline; tracking #397)
======================================================================

A Claude-Code-style checkpoint/rewind for the build. Before each wave/subtask the
self-heal loop takes a ``snapshot`` of the task branch HEAD; if the unit can't be
salvaged (retries exhausted, verifier still failing), it ``rollback``s to the
last good checkpoint so a confidently-wrong agent can't pile churn onto the
branch. Checkpoints are recorded in the build report (#397 Phase 1 slots).

Pure git over subprocess — no new deps. A snapshot is just the current commit
SHA; rollback is ``git reset --hard <sha>`` + ``git clean -fd`` (to also drop
untracked files the failed unit created). The coder commits per subtask, so HEAD
is a meaningful restore point between units.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """A restorable point on the task branch."""

    ref: str  # commit SHA
    label: str  # e.g. "pre-wave-1" / "pre-subtask-st3"


class Checkpointer:
    """Takes and restores git checkpoints on a build's task worktree."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir)
        self.checkpoints: list[Checkpoint] = []

    def _git(self, *args: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return proc.returncode, (proc.stdout or proc.stderr).strip()
        except OSError as exc:  # git missing / cwd gone
            return 1, str(exc)

    def current_head(self) -> str | None:
        rc, out = self._git("rev-parse", "HEAD")
        return out if rc == 0 and out else None

    def snapshot(self, label: str) -> Checkpoint | None:
        """Record the current HEAD as a named checkpoint (None if unavailable)."""
        head = self.current_head()
        if not head:
            logger.debug("checkpoint snapshot skipped: no HEAD in %s", self.project_dir)
            return None
        cp = Checkpoint(ref=head, label=label)
        self.checkpoints.append(cp)
        logger.info("[checkpoint] %s @ %s", label, head[:8])
        return cp

    def rollback(self, checkpoint: Checkpoint) -> bool:
        """Hard-reset the task branch to ``checkpoint`` and drop untracked files.

        Returns True on success. Best-effort: a failure is logged and reported,
        never raised, so the self-heal loop can escalate cleanly.
        """
        rc, out = self._git("reset", "--hard", checkpoint.ref)
        if rc != 0:
            logger.error("[checkpoint] rollback reset failed: %s", out)
            return False
        # Drop untracked files/dirs the failed unit created (e.g. half-written
        # modules) so the restored state is exactly the checkpoint.
        self._git("clean", "-fd")
        logger.info("[checkpoint] rolled back to %s (%s)", checkpoint.label, checkpoint.ref[:8])
        return True

    def latest(self) -> Checkpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def as_report(self) -> list[dict]:
        """Checkpoint list for build_report.json (#397)."""
        return [{"label": c.label, "ref": c.ref} for c in self.checkpoints]
