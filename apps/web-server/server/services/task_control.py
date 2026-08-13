"""Control-plane state store for tasks (Issue #259).

Board column / task status and review status (``reviewReason``) are
*control-plane* state: they are set by humans (kanban drag-drop, plan
approval) and by the web-server's own orchestration (phase->status
mapping at terminal checkpoints). They are NOT agent artifacts.

Historically this state lived inside ``implementation_plan.json`` — the
same file the agent rewrites in its isolated worktree. Every worktree
sync therefore risked clobbering human/system control state, which is
why ``agent_service._sync_worktree_files`` grew a fragile
"merge-during-copy" workaround that preserved ``status`` /
``reviewReason`` and fell back to a raw copy on any error.

This module gives control-plane state a dedicated home —
``.aifactory/specs/<id>/task_control.json`` — that:

  * is written ONLY by the web-server (never by agents),
  * is NEVER part of any worktree sync file list, so an agent's
    worktree copy can no longer reset it,
  * is written atomically (tmp file + ``os.replace``) so a crash
    mid-write can't leave a half-written control file,
  * carries a read-time migration: when the dedicated file is absent
    but a legacy ``implementation_plan.json`` already holds ``status``
    / ``reviewReason``, those are surfaced so existing specs keep
    working without a data migration step.

The store is intentionally tiny and dependency-free so it can be
imported from both route handlers and the agent service.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory_common.logsafe import sanitize_log

logger = logging.getLogger(__name__)

# Dedicated control-plane file name. Lives alongside implementation_plan.json
# in the *main* spec dir, and is deliberately excluded from every worktree
# sync file list so agents can never write or overwrite it.
CONTROL_FILE_NAME = "task_control.json"

# The control-plane fields that this store owns. These are the fields that
# must be stripped from any agent-synced artifact so a worktree copy can't
# reset them.
CONTROL_FIELDS = ("status", "reviewReason")


def control_file_path(spec_dir: Path) -> Path:
    """Return the path to the control-plane file for a spec dir."""
    return spec_dir / CONTROL_FILE_NAME


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_control(spec_dir: Path) -> dict[str, Any]:
    """Read the control-plane state for a spec.

    Returns a dict that may contain ``status``, ``reviewReason``,
    ``updatedAt`` and ``updatedBy``. Returns an empty dict when there is
    no control state to surface.

    Read-time migration: if ``task_control.json`` is absent, fall back to
    any ``status`` / ``reviewReason`` already persisted in the legacy
    ``implementation_plan.json`` so pre-#259 specs keep their human/system
    state. The fallback is read-only — it does not write the new file
    (callers that mutate control state will create it on next write).
    """
    cfile = control_file_path(spec_dir)
    if cfile.exists():
        try:
            data = json.loads(cfile.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "[task_control] Failed to read %s, falling back to legacy: %s",
                sanitize_log(cfile),
                sanitize_log(e),
            )

    # Backward-compat read-time migration from implementation_plan.json.
    legacy = spec_dir / "implementation_plan.json"
    if legacy.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            plan = json.loads(legacy.read_text())
            if isinstance(plan, dict):
                migrated: dict[str, Any] = {}
                for field in CONTROL_FIELDS:
                    if field in plan and plan[field] is not None:
                        migrated[field] = plan[field]
                if migrated:
                    migrated["_migratedFromPlan"] = True
                    return migrated

    return {}


def write_control(
    spec_dir: Path,
    *,
    status: str | None = None,
    review_reason: str | None = None,
    clear_review_reason: bool = False,
    updated_by: str = "web_server",
) -> dict[str, Any]:
    """Atomically persist control-plane state for a spec.

    Only the web-server should ever call this. The write merges over any
    existing control state so partial updates (status-only, or
    reviewReason-only) are safe.

    Args:
        spec_dir: The *main* spec dir (never a worktree dir).
        status: New board column / task status. ``None`` leaves it
            unchanged.
        review_reason: New review reason. ``None`` leaves it unchanged
            unless ``clear_review_reason`` is set.
        clear_review_reason: When True, explicitly removes ``reviewReason``
            (used when moving a task out of a human-review column).
        updated_by: Audit tag for who/what made the change.

    Returns the full control-state dict that was written.
    """
    spec_dir.mkdir(parents=True, exist_ok=True)

    current = read_control(spec_dir)
    # Drop the migration marker so it never gets persisted.
    current.pop("_migratedFromPlan", None)

    if status is not None:
        current["status"] = status
    if clear_review_reason:
        current.pop("reviewReason", None)
    elif review_reason is not None:
        current["reviewReason"] = review_reason

    current["updatedAt"] = _now_iso()
    current["updatedBy"] = updated_by

    _atomic_write_json(control_file_path(spec_dir), current)
    return current


def strip_control_fields(plan: dict[str, Any]) -> dict[str, Any]:
    """Remove control-plane fields from an agent artifact dict in place.

    Used on the worktree-sync path: the agent's ``implementation_plan.json``
    may carry stale ``status`` / ``reviewReason`` values (or none at all),
    and we must never let those land in the main spec's plan file where the
    reader might pick them up over the dedicated control store.
    """
    for field in CONTROL_FIELDS:
        plan.pop(field, None)
    return plan


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically: tmp file in the same dir + ``os.replace``.

    Writing to a temp file in the same directory and then replacing the
    target guarantees readers never observe a partially written file, and
    that a crash mid-write leaves the previous good file intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
