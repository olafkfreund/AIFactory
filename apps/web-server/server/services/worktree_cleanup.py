"""Remove vetted orphan registrations without touching their working directories."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from factory_common.logsafe import sanitize_log

from server.services import build_backend, stale_tasks, task_control
from server.specpath import spec_dir_for

logger = logging.getLogger(__name__)


def cleanup_orphaned_worktrees(
    project_path: Path, project_id: str, active_job_ids: set[str]
) -> list[str]:
    """Return still-orphaned paths; delete only safe, inactive registrations."""
    candidates = build_backend.orphaned_worktree_registrations(project_path)
    if not candidates:
        return []
    removed: set[str] = set()
    try:
        root = (project_path / ".aifactory" / "worktrees").resolve()
        common = build_backend._git_out(
            ["-C", str(project_path), "rev-parse", "--git-common-dir"]
        )
        if not common:
            raise ValueError("git common directory unavailable")
        admin_root = (project_path / common).resolve() / "worktrees"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        logger.error(
            "Cleanup failed for %s: %s", sanitize_log(project_path), sanitize_log(exc)
        )
        logger.info("removed=%d deferred=%d", 0, len(candidates))
        return candidates

    for candidate in candidates:
        try:
            kind, spec = Path(candidate).resolve().relative_to(root).parts
            if kind not in {"tasks", "builds"}:
                raise ValueError("unsafe worktree path")
            spec_dir = spec_dir_for(project_path, spec)
            if spec in active_job_ids or f"{project_id}:{spec}" in active_job_ids:
                continue
            if spec_dir.exists():
                control = task_control.control_file_path(spec_dir)
                # A corrupt control file must never fall back to stale legacy state.
                if control.exists() and not isinstance(
                    json.loads(control.read_text()), dict
                ):
                    raise ValueError("invalid task control object")
                status = task_control.read_control(spec_dir).get("status")
                if (
                    not isinstance(status, str)
                    or status not in stale_tasks.TERMINAL_STATES
                ):
                    continue
            admin = next(
                (
                    gitdir.parent
                    for gitdir in admin_root.glob("*/gitdir")
                    if gitdir.read_text().strip().removesuffix("/.git") == candidate
                ),
                None,
            )
            if admin is None:
                raise ValueError("matching worktree registration unavailable")
            if (admin / "locked").exists():
                continue
            shutil.rmtree(admin)
            removed.add(candidate)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            logger.error(
                "Skipped unsafe or failed cleanup %s: %s",
                sanitize_log(candidate),
                sanitize_log(exc),
            )

    remaining = [candidate for candidate in candidates if candidate not in removed]
    logger.info("removed=%d deferred=%d", len(removed), len(remaining))
    return remaining
