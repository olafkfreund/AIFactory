"""Detect tasks whose worker died and left them looking active.

A task's status is written BY the worker running it. If that worker dies -- pod
evicted, Job reaped, OOM, node drained -- nothing writes the terminal status,
and the record stays in a machine-owned state forever. The cockpit then shows
it under "Active" indefinitely, which is faithful reporting of a record that is
wrong.

Observed live: `089-memory-chain-probe-three` sat at `in_progress` / `planning`
for 27 hours with no task pod anywhere in the cluster.

## The distinction that makes this safe

Two states look identical to a clock and must never be treated alike:

* **Machine-owned** (`in_progress`, `backlog`, `ai_review`) -- something is
  supposed to be working. Silence means the worker is gone.
* **Human-owned** (`human_review`) -- a person is supposed to act. Silence
  means nobody has looked yet, which is not a fault at any age. Three tasks on
  the live board have waited 19-38 hours legitimately.

Reaping on age alone would destroy real work awaiting review. Human-owned
states are therefore never stale, however old.

## Why mark rather than delete

A reaped task becomes `failed` with a recorded reason. Deleting would remove
the evidence of what died and why, and an orphan is a signal about the
execution layer worth keeping. Marking also lets it drop out of "Active"
without pretending it succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# States where a MACHINE owes the next move. Silence here is a stall.
MACHINE_OWNED_STATES: frozenset[str] = frozenset(
    {"in_progress", "backlog", "ai_review", "queued", "running"}
)

# States where a HUMAN owes the next move. Never stale, at any age.
HUMAN_OWNED_STATES: frozenset[str] = frozenset({"human_review", "review"})

# Terminal: nothing owes anything.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"done", "completed", "failed", "cancelled", "qa_failed"}
)

# How long a machine-owned task may go without any write before it is presumed
# orphaned. Generous on purpose: a slow build legitimately goes quiet for a
# while, and a false reap kills real work, whereas a late reap only means an
# orphan lingers a bit longer. Asymmetric costs, asymmetric default.
DEFAULT_STALE_AFTER = timedelta(hours=4)


@dataclass(frozen=True)
class StaleTask:
    """One task presumed orphaned, with the evidence for saying so."""

    task_id: str
    status: str
    phase: str | None
    updated_at: str
    idle_hours: float

    def reason(self) -> str:
        return (
            f"orphaned: status '{self.status}' is machine-owned but nothing has "
            f"written to this task for {self.idle_hours:.1f}h "
            f"(last activity {self.updated_at}). No worker is running it."
        )


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def is_reapable(status: str) -> bool:
    """Is *status* one where silence indicates a dead worker?

    Anything unrecognised is treated as NOT reapable. A new status the reaper
    has not been taught about must not be destroyed by it -- the failure mode
    of guessing wrong here is deleting live work.
    """
    return status in MACHINE_OWNED_STATES


def find_stale(
    tasks: list[dict[str, Any]],
    *,
    now: datetime,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> list[StaleTask]:
    """Tasks presumed orphaned. Pure: no I/O, no clock, no network.

    ``now`` is injected so the caller controls time and tests are deterministic.
    """
    out: list[StaleTask] = []
    for task in tasks:
        status = str(task.get("status") or "")
        if not is_reapable(status):
            continue

        raw = str(task.get("updated_at") or task.get("updatedAt") or "")
        updated = _parse(raw)
        if updated is None:
            # An unparseable timestamp is not evidence of death. Skip it rather
            # than assume the worst about a task we cannot actually date.
            continue

        # Normalise to naive UTC before subtracting. Task timestamps come from
        # file mtime and are naive; a caller may pass an aware `now`. Mixing
        # them raises, and the previous form of this had identical branches -
        # it read like it handled the case and did not.
        reference = now.replace(tzinfo=None) if now.tzinfo else now
        updated_naive = updated.replace(tzinfo=None) if updated.tzinfo else updated

        idle = reference - updated_naive
        if idle < stale_after:
            continue

        out.append(
            StaleTask(
                task_id=str(task.get("id") or ""),
                status=status,
                phase=task.get("phase"),
                updated_at=raw,
                idle_hours=idle.total_seconds() / 3600.0,
            )
        )
    return out


def summarise(stale: list[StaleTask], *, dry_run: bool) -> dict[str, Any]:
    """A report a human or a CronJob can act on."""
    return {
        "stale_count": len(stale),
        "dry_run": dry_run,
        "action": "reported" if dry_run else "marked failed",
        "tasks": [
            {
                "id": s.task_id,
                "status": s.status,
                "phase": s.phase,
                "idle_hours": round(s.idle_hours, 1),
                "reason": s.reason(),
            }
            for s in stale
        ],
    }
