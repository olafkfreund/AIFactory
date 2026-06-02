"""
Review Re-Drive Service — Web-Server Side Wiring (#260)
======================================================

Bridges the web-server's periodic task tick to the backend's review re-drive
orchestrator (``qa.review_redrive.process_untouched_review``). When a review
request has gone untouched, this drives it forward: first an inbox nudge to the
running reviewer, then escalation to human review.

Why a dedicated service (not a direct ``from qa.review_redrive import ...``)?
    Importing the ``qa`` *package* would run ``qa/__init__.py``, which pulls in
    the Claude Agent SDK and other agent deps — heavy, and unavailable without
    OAuth. The two modules we need (``review_cycle``, ``review_redrive``) are
    pure stdlib. We therefore load them directly from file via ``importlib``
    under their canonical ``qa.*`` names, WITHOUT triggering ``qa/__init__``.

Dependency injection
    ``process_untouched_review`` takes the inbox-enqueue and control-plane-write
    functions as parameters. We pass the web-server's own
    ``inbox_service.enqueue`` and ``task_control.write_control`` — so the backend
    package never imports web-server code and the strike logic stays testable.

Spec-dir resolution (worktree vs main)
    * cycle + control state authority → MAIN spec dir
      ``<project>/.aifactory/specs/<spec_id>``
    * inbox nudge delivery (running reviewer drains here) → WORKTREE spec dir
      ``<project>/.aifactory/worktrees/tasks/<spec_id>/.aifactory/specs/<spec_id>``
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from ..config import get_settings
from . import inbox_service, task_control

logger = logging.getLogger(__name__)

# Lazily-loaded backend leaf modules (pure stdlib, no SDK).
_review_redrive_mod: ModuleType | None = None
_load_failed = False


def _load_module_from_file(module_name: str, file_path: Path) -> ModuleType:
    """Import a single module from an explicit file path under ``module_name``.

    Registers the module in ``sys.modules`` under its canonical name so that a
    sibling's relative import (``from .review_cycle import ...``) resolves
    against it without executing the package ``__init__``.
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _get_redrive_module() -> ModuleType | None:
    """Load ``qa.review_redrive`` (and its ``qa.review_cycle`` dependency) once.

    Returns ``None`` (and disables further attempts) if the backend modules
    cannot be found — the poll tick must never crash the task monitor.
    """
    global _review_redrive_mod, _load_failed
    if _review_redrive_mod is not None:
        return _review_redrive_mod
    if _load_failed:
        return None

    try:
        settings = get_settings()
        backend_path = Path(settings.BACKEND_PATH).resolve()
        qa_dir = backend_path / "qa"
        cycle_file = qa_dir / "review_cycle.py"
        redrive_file = qa_dir / "review_redrive.py"
        if not cycle_file.exists() or not redrive_file.exists():
            raise FileNotFoundError(f"review modules missing under {qa_dir}")

        # Register a minimal synthetic ``qa`` package so the relative import in
        # review_redrive resolves WITHOUT running the real qa/__init__ (SDK).
        if "qa" not in sys.modules:
            pkg = ModuleType("qa")
            pkg.__path__ = [str(qa_dir)]  # type: ignore[attr-defined]
            sys.modules["qa"] = pkg

        # review_cycle must be importable as qa.review_cycle before review_redrive.
        if "qa.review_cycle" not in sys.modules:
            _load_module_from_file("qa.review_cycle", cycle_file)
        _review_redrive_mod = _load_module_from_file(
            "qa.review_redrive", redrive_file
        )
        logger.debug("[review_redrive] loaded backend review modules")
        return _review_redrive_mod
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash poll
        _load_failed = True
        logger.warning("[review_redrive] disabled (load failed): %s", exc)
        return None


def _main_spec_dir(project_path: Path, spec_id: str) -> Path:
    return project_path / ".aifactory" / "specs" / spec_id


def _worktree_spec_dir(project_path: Path, spec_id: str) -> Path:
    return (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".aifactory"
        / "specs"
        / spec_id
    )


def check_review_obligation(
    project_path: Path,
    spec_id: str,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Run one re-drive tick for a task; nudge or escalate if a review is stuck.

    Safe to call every poll tick: it is idempotent within the back-off window and
    returns ``None`` when no action is due. Never raises — any failure is logged
    and swallowed so the task monitor keeps running.

    Returns the action record (``{"action": "nudged"|"escalated", ...}``) when a
    strike was taken, else ``None``.
    """
    mod = _get_redrive_module()
    if mod is None:
        return None

    main_spec = _main_spec_dir(project_path, spec_id)
    # No cycle file yet → nothing to drive; cheap early-out avoids module work.
    if not (main_spec / "qa_review_cycle.json").exists():
        return None
    worktree_spec = _worktree_spec_dir(project_path, spec_id)

    kwargs: dict[str, Any] = {
        "enqueue": inbox_service.enqueue,
        "write_control": task_control.write_control,
    }
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds

    try:
        result = mod.process_untouched_review(
            main_spec,
            worktree_spec if worktree_spec.exists() else None,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - never crash the poll tick
        logger.warning(
            "[review_redrive] process failed for %s: %s", spec_id, exc
        )
        return None

    if result:
        logger.info(
            "[review_redrive] %s for spec=%s cycle=%s",
            result.get("action"),
            spec_id,
            result.get("cycle_id"),
        )
    return result
