"""Task helper/service functions — extracted from routes/tasks.py (#556 split).

The spec-directory / worktree / plan / serialization helpers that the task route
handlers (and sub-routers) rely on. Moved out of routes/tasks.py so the file is
a thin routing layer; routes/tasks.py re-exports every name here, so existing
``from ..routes.tasks import spec_to_task`` callers are unchanged. This module
imports models from routes/task_models (never from routes/tasks) so there is no
circular import.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from server.services.task_branch import recorded_branch
from server.services.task_status import PLAN_UNREADABLE, read_plan
from server.specpath import safe_spec_component

logger = logging.getLogger(__name__)


from ..services import task_control
from .projects import load_projects, resolve_project_path
from .task_models import (
    Subtask,
    SubtaskVerification,
    Task,
    TaskMetadata,
    TaskStatus,
)


def _resolve_task(task_id: str) -> tuple[str, str, Path, Path]:
    """Resolve task_id (projectId:specId) to project_id, spec_id, project_path, spec_dir.

    Canonical single definition (#769). Previously duplicated byte-for-byte in
    routes/tasks.py and routes/inbox.py; both now import it from here. Because
    resolution reads ``task_service.load_projects``, tests that need to stub the
    project map patch ``server.routes.task_service.load_projects`` (the one
    canonical seam) rather than each route module's copy.

    Raises HTTPException on invalid input or missing resources.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=400, detail="Invalid task_id format (expected projectId:specId)"
        )

    project_id, spec_id = task_id.split(":", 1)

    # Barrier BEFORE spec_id reaches any path expression (#1056). Path joins
    # collapse traversal silently, so validating after the join is too late.
    # This is the canonical seam for the projectId:specId parse, so validating
    # here covers every caller that uses it rather than each one separately.
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid task_id format (expected projectId:specId)"
        ) from None

    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(status_code=404, detail="Project not found")

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id

    if not spec_dir.exists():
        raise HTTPException(status_code=404, detail="Task spec not found")

    return project_id, spec_id, project_path, spec_dir


def get_spec_dirs(project_path: Path) -> list[Path]:
    """Get all spec directories in a project."""
    specs_dir = project_path / ".aifactory" / "specs"
    if not specs_dir.exists():
        return []
    return sorted([d for d in specs_dir.iterdir() if d.is_dir()])


def get_next_spec_id(project_path: Path, title: str) -> str:
    """Generate the next spec ID (e.g., '003-feature-name').

    Uses a counter file (.aifactory/specs/.counter) to ensure IDs
    never get reused after deletion.
    """
    specs_dir = project_path / ".aifactory" / "specs"
    counter_file = specs_dir / ".counter"

    # Read persisted counter (highest ID ever assigned)
    persisted_max = 0
    if counter_file.exists():
        try:
            persisted_max = int(counter_file.read_text().strip())
        except (ValueError, OSError):
            pass

    # Also check existing directories in case counter file is missing
    existing = get_spec_dirs(project_path)
    dir_max = 0
    for spec_dir in existing:
        match = re.match(r"(\d+)-", spec_dir.name)
        if match:
            dir_max = max(dir_max, int(match.group(1)))

    # Use the higher of persisted counter and directory scan
    max_num = max(persisted_max, dir_max)
    next_num = max_num + 1

    # Persist the new counter
    specs_dir.mkdir(parents=True, exist_ok=True)
    counter_file.write_text(str(next_num))

    # Generate slug from title
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30]

    # Fallback to "untitled-task" if slug is empty
    if not slug:
        slug = "untitled-task"

    # The slug above is a best-effort `re.sub` over a request-supplied title,
    # and the returned id is joined straight onto the specs root by every
    # caller. Assert the result really is a safe component before it becomes a
    # path: `re.sub` narrows, it does not guarantee, and this id is the single
    # origin of every spec_dir the readers later walk. (Also the barrier CodeQL
    # recognises -- it models a `fullmatch` and not a `sub`, which is why the
    # slug alone left the whole downstream flow tainted.) Cannot raise: the
    # slug is `[a-z0-9-]` and the prefix is digits.
    return safe_spec_component(f"{next_num:03d}-{slug}")


def get_worktree_spec_dir(project_path: Path, spec_id: str) -> Path | None:
    """Get the worktree spec directory if it exists.

    Worktree layout: .aifactory/worktrees/tasks/{spec_id}/.aifactory/specs/{spec_id}/

    Barriers its own argument rather than trusting callers to have done it.
    Every caller today does, but "every caller today" is the assumption that
    produced #1056 in the first place, and this helper is one join away from a
    filesystem read.
    """
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        return None
    worktree_spec_dir = (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".aifactory"
        / "specs"
        / spec_id
    )
    if worktree_spec_dir.exists():
        return worktree_spec_dir
    return None


def sync_worktree_to_main_spec(project_path: Path, spec_id: str) -> bool:
    """Sync implementation_plan.json from worktree to main spec if worktree has newer data.

    Returns True if sync was performed, False otherwise.
    """
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        return False
    main_spec_dir = project_path / ".aifactory" / "specs" / spec_id
    worktree_spec_dir = get_worktree_spec_dir(project_path, spec_id)

    if not worktree_spec_dir:
        return False

    worktree_plan_file = worktree_spec_dir / "implementation_plan.json"
    main_plan_file = main_spec_dir / "implementation_plan.json"

    if not worktree_plan_file.exists():
        return False

    try:
        worktree_plan = json.loads(worktree_plan_file.read_text())
        main_plan = {}
        if main_plan_file.exists():
            main_plan = json.loads(main_plan_file.read_text())

        # Count completed subtasks in each plan
        def count_completed(plan: dict) -> int:
            count = 0
            for phase in plan.get("phases", []):
                for subtask in phase.get("subtasks", []):
                    if subtask.get("status") == "completed":
                        count += 1
            return count

        worktree_completed = count_completed(worktree_plan)
        main_completed = count_completed(main_plan)

        # Only sync if worktree has more progress (more completed subtasks)
        if worktree_completed > main_completed:
            import logging

            logger = logging.getLogger(__name__)
            logger.info(
                f"[WorktreeSync] Syncing plan for {spec_id}: "
                f"worktree has {worktree_completed} completed vs main {main_completed}"
            )
            # Issue #259: control-plane state lives in task_control.json, NOT in
            # the agent-written plan. Strip status/reviewReason from the worktree
            # copy so a sync can never reset the human/system control decision.
            task_control.strip_control_fields(worktree_plan)
            main_plan_file.write_text(json.dumps(worktree_plan, indent=2))
            return True

        return False
    except (json.JSONDecodeError, OSError) as e:
        import logging

        logging.getLogger(__name__).warning(
            f"[WorktreeSync] Failed to sync {spec_id}: {e}"
        )
        return False


def validate_done_status(plan: dict) -> tuple[bool, str]:
    """Validate that all subtasks are completed before allowing 'done' status.

    Returns (is_valid, error_message).
    """
    phases = plan.get("phases", [])
    if not phases:
        # No phases means no subtasks to validate
        return True, ""

    total_subtasks = 0
    completed_subtasks = 0

    for phase in phases:
        for subtask in phase.get("subtasks", []):
            total_subtasks += 1
            if subtask.get("status") == "completed":
                completed_subtasks += 1

    if total_subtasks == 0:
        return True, ""

    if completed_subtasks < total_subtasks:
        return False, (
            f"Cannot mark as done: only {completed_subtasks}/{total_subtasks} "
            f"subtasks are completed. Complete all subtasks first or check if "
            f"worktree has newer progress."
        )

    return True, ""


def get_plan_with_worktree_sync(
    project_path: Path, spec_id: str
) -> tuple[dict, Path, str | None]:
    """Get implementation plan, syncing from worktree first if needed.

    Returns ``(plan_dict, plan_file_path, read_error)``. ``read_error`` is None
    when the plan parsed (including when there is no plan file at all — absent
    is not corrupt), and carries the parse location otherwise.

    The error is RETURNED rather than collapsed into the empty dict (#1081): the
    write callers cannot tell "the plan is empty" from "the plan could not be
    read" without it, and they used to treat both as a licence to replace the
    file with a single ``{"status": ...}`` key.
    """
    # Barrier BEFORE spec_id reaches any path expression (#1056). This helper
    # takes spec_id as a raw string and joins it onto a trusted root; both
    # callers happen to validate first, but the join lives HERE, so the check
    # belongs here too rather than depending on every caller remembering.
    spec_id = safe_spec_component(spec_id)

    # Sync worktree to main spec first
    sync_worktree_to_main_spec(project_path, spec_id)

    # Read from main spec (now potentially updated)
    main_spec_dir = project_path / ".aifactory" / "specs" / spec_id
    plan_file = main_spec_dir / "implementation_plan.json"

    plan: dict[str, Any] = {}
    error: str | None = None
    if plan_file.exists():
        # #1069: read_plan logs the path and the parse offset. It used to be a
        # bare ``pass``, which is how an unparseable plan reached the callers
        # below as an empty dict.
        plan, error = read_plan(plan_file)

    return plan, plan_file, error


def reject_if_plan_unreadable(plan_file: Path, error: str | None) -> None:
    """Refuse a plan-mutating request when the plan could not be read (#1081).

    "I cannot read this" is never a licence to replace it. Both kanban status
    writers used to persist the empty dict ``read_plan`` returns on a parse
    failure, collapsing phases, subtasks and verification into a single
    ``{"status": ...}`` key — the corrupt file was at least still diagnosable.
    The same empty dict also made ``validate_done_status`` vacuous (no phases
    means nothing to check), so ``done`` was approved for a task nobody could
    evaluate.

    Raises 409 naming the file and the parse location, so the operator learns
    the plan needs regenerating instead of silently losing it. No-op when the
    plan parsed, or when there is no plan file at all.
    """
    if error is None:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"{plan_file} could not be read ({error}). The status change was "
            "not applied and the file was left untouched — regenerate the plan."
        ),
    )


# Keys preferred (in order) when collapsing a stringified mapping down to a
# single readable line for the cockpit Overview.
_DESC_PREFERRED_KEYS = (
    "description",
    "summary",
    "brief",
    "text",
    "title",
    "epic_title",
    "plan_id",
)

# Matches an embedded ``Correlation epic #{...}`` run where a whole dict was
# str()-ed into otherwise-clean prose. Non-greedy + DOTALL so it collapses a
# multi-line dict body while leaving the surrounding prose intact.
_CORRELATION_EPIC_DICT_RE = re.compile(r"Correlation epic #\{.*?\}", re.DOTALL)

# Best-effort extraction of a plan_id out of such an embedded dict run.
_PLAN_ID_RE = re.compile(r"['\"]plan_id['\"]\s*:\s*['\"]([^'\"]+)['\"]")

# Max length of the single-line fallback when a stringified mapping cannot be
# parsed, so the cockpit never receives a wall of (collapsed) dict text.
_FALLBACK_MAX_LEN = 200


def _collapse_correlation_epic(desc: str) -> str:
    """Collapse an embedded ``Correlation epic #{...dict...}`` run.

    Replaces the ``#{...}`` blob with ``#<plan_id>`` when a plan_id can be
    found, otherwise with a bare ``Correlation epic`` reference. Prose around
    the blob is preserved.
    """

    def _replace(match: re.Match[str]) -> str:
        plan_match = _PLAN_ID_RE.search(match.group(0))
        if plan_match:
            return f"Correlation epic #{plan_match.group(1)}"
        return "Correlation epic"

    return _CORRELATION_EPIC_DICT_RE.sub(_replace, desc)


def _looks_like_stringified_mapping(stripped: str) -> bool:
    """Conservatively detect a value that is a whole dict str()-ed to text.

    Requires the value to both start with ``{`` and end with ``}`` and contain
    a mapping-style ``key:`` separator (``':`` for Python reprs / single-quoted
    JSON, or ``": `` for JSON). Prose with a stray brace is left untouched.
    """
    return (
        stripped.startswith("{")
        and stripped.endswith("}")
        and ("':" in stripped or '": ' in stripped)
    )


def _summarize_mapping(mapping: dict) -> str:
    """Pick a short readable string out of a parsed mapping.

    Prefers a human-readable field (description/summary/brief/text), then a
    title-like field, then plan_id. Only ever returns a scalar string value —
    never anything dict/list-shaped — so the cockpit cannot render dict guts.
    """
    for key in _DESC_PREFERRED_KEYS:
        value = mapping.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return ""


def _clean_task_description(desc: str) -> str:
    """Defensive guard so the cockpit Overview never renders dict guts.

    The common case (normal markdown / prose) is returned unchanged. This is a
    belt-and-suspenders product guard against upstream data that str()-ed a
    whole plan/epic dict into the task description (see aifactory-demo#206,
    AIFactory#612). It never raises.
    """
    if not isinstance(desc, str):
        return desc

    stripped = desc.strip()
    if not stripped:
        return desc

    # Case 1: the entire description IS a stringified dict/JSON mapping.
    if _looks_like_stringified_mapping(stripped):
        parsed: object = None
        for loader in (json.loads, ast.literal_eval):
            try:
                parsed = loader(stripped)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                continue
            else:
                break

        if isinstance(parsed, dict):
            summary = _summarize_mapping(parsed)
            if summary:
                return summary

        # Parsing failed (or yielded nothing usable): collapse to a single
        # line, strip the outer braces, and truncate so no multi-line dict
        # guts ever reach the renderer.
        flattened = " ".join(stripped.strip("{}").split())
        if len(flattened) > _FALLBACK_MAX_LEN:
            flattened = flattened[:_FALLBACK_MAX_LEN].rstrip() + "…"
        return flattened

    # Case 2: clean prose that merely embeds a ``Correlation epic #{...}`` run.
    if "Correlation epic #{" in desc:
        return _collapse_correlation_epic(desc)

    return desc


_VALID_SUBTASK_STATUS = {"pending", "in_progress", "completed", "failed", "blocked"}
# Out-of-enum status values a planner (LLM or upstream stage) may persist, mapped
# to the nearest valid one. Anything unrecognized falls back to "pending" so a
# single malformed spec can never 500 the whole task list (#942).
_SUBTASK_STATUS_SYNONYMS = {
    "ready": "pending",
    "todo": "pending",
    "not_started": "pending",
    "queued": "pending",
    "running": "in_progress",
    "active": "in_progress",
    "in-progress": "in_progress",
    "wip": "in_progress",
    "done": "completed",
    "complete": "completed",
    "success": "completed",
    "passed": "completed",
    "error": "failed",
    "errored": "failed",
}


def _coerce_subtask_status(raw: object) -> str:
    """Normalize a persisted subtask status to a valid Subtask literal.

    ``Subtask.status`` is a strict ``Literal`` of five values; feeding it any
    other string raises a ValidationError. Plans on disk sometimes carry
    out-of-enum values (an LLM planner emits ``"ready"``), and one such spec
    would otherwise crash ``GET /api/tasks`` for every task. Map known synonyms,
    default the rest to ``"pending"``.
    """
    s = str(raw or "pending").strip().lower()
    if s in _VALID_SUBTASK_STATUS:
        return s
    return _SUBTASK_STATUS_SYNONYMS.get(s, "pending")


def load_spec_metadata(spec_dir: Path) -> dict:
    """Load metadata for a spec from its files."""
    metadata = {
        "title": spec_dir.name,
        "description": "",
        "status": "backlog",
        "phase": None,
        "subtasks": [],
        "worktree_path": None,
        "branch_name": None,
        "archivedAt": None,
        "archivedInVersion": None,
        "reviewReason": None,
        "github_issue": None,
    }

    # Try to load requirements.json for title/description (most accurate source)
    requirements_file = spec_dir / "requirements.json"
    if requirements_file.exists():
        try:
            requirements = json.loads(requirements_file.read_text())
            if "title" in requirements:
                metadata["title"] = requirements["title"]
            if "description" in requirements:
                metadata["description"] = _clean_task_description(
                    requirements["description"]
                )
            # RFC-0001 correlation: surface the upstream GitHub issue so the
            # cockpit threads this task with its PFactory plan + TFactory test.
            prov = requirements.get("provenance")
            if isinstance(prov, dict) and prov.get("issue_number") is not None:
                metadata["github_issue"] = prov.get("issue_number")
        except (json.JSONDecodeError, KeyError):
            pass

    # Fall back to spec.md if requirements.json not available
    if not metadata["description"]:
        spec_file = spec_dir / "spec.md"
        if spec_file.exists():
            content = spec_file.read_text()
            # Extract title from first # heading if not already set
            if not metadata["title"] or metadata["title"] == spec_dir.name:
                title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                if title_match:
                    metadata["title"] = title_match.group(1)
            # Use first paragraph as description (no truncation)
            paragraphs = re.split(r"\n\n+", content)
            for p in paragraphs[1:]:  # Skip title
                if p.strip() and not p.startswith("#"):
                    metadata["description"] = _clean_task_description(p.strip())
                    break

    # Try to load task_logs.json for active phase status (most accurate)
    task_logs_file = spec_dir / "task_logs.json"
    if task_logs_file.exists():
        try:
            logs = json.loads(task_logs_file.read_text())
            phases = logs.get("phases", {})

            # First check for any active phase
            has_active_phase = False
            for phase_name, phase_data in phases.items():
                if phase_data.get("status") == "active":
                    metadata["phase"] = phase_name
                    metadata["status"] = "in_progress"
                    has_active_phase = True
                    break

            # If no active phase, check for terminal states
            if not has_active_phase:
                # Check if any phase failed → task needs human intervention
                has_failed_phase = any(
                    phase_data.get("status") == "failed"
                    for phase_data in phases.values()
                )
                if has_failed_phase:
                    metadata["status"] = "human_review"
                    metadata["reviewReason"] = "errors"
                else:
                    # Check validation phase completed (strongest completion signal)
                    validation_phase = phases.get("validation", {})
                    if validation_phase.get(
                        "status"
                    ) == "completed" and validation_phase.get("entries"):
                        metadata["phase"] = "validation"
                        metadata["status"] = "human_review"
                        metadata["reviewReason"] = "completed"
                    else:
                        # Fall back to coding phase completed
                        coding_phase = phases.get("coding", {})
                        if coding_phase.get(
                            "status"
                        ) == "completed" and coding_phase.get("entries"):
                            metadata["phase"] = "coding"
                            metadata["status"] = "human_review"
                            metadata["reviewReason"] = "completed"
        except (json.JSONDecodeError, KeyError):
            pass

    # Try to load implementation_plan.json for status/subtasks
    plan_file = spec_dir / "implementation_plan.json"
    explicit_status = None  # Track if user explicitly set status via kanban
    plan_unreadable = False
    if plan_file.exists():
        # #1069: parse OUTSIDE the tolerant handler below. That handler exists to
        # degrade gracefully when part of a well-formed plan is odd (#941); a file
        # that does not parse at all is a different thing — a fault — and must be
        # remembered rather than silently becoming an empty plan.
        plan, plan_error = read_plan(plan_file)
        plan_unreadable = plan_error is not None
        try:
            # Only set phase from plan if not already set from task_logs
            if not metadata["phase"]:
                metadata["phase"] = plan.get("phase")

            # If no explicit phase, try to detect from phases array
            if not metadata["phase"] and "phases" in plan:
                for phase in plan["phases"]:
                    if isinstance(phase, dict):
                        phase_status = phase.get("status", "")
                        if phase_status == "in_progress":
                            metadata["phase"] = phase.get("name", phase.get("id"))
                            break

            # Check if status was explicitly set (kanban drag-drop saves this)
            # "done" and "completed" statuses ALWAYS take precedence (task was explicitly finished)
            # Other statuses only apply if we didn't already detect active status from task_logs
            if "status" in plan:
                explicit_status = plan["status"]
                if explicit_status in ("done", "completed"):
                    # Task was explicitly marked as done - always honor this
                    metadata["status"] = explicit_status
                elif metadata["status"] == "backlog":
                    # Only override backlog with other statuses
                    metadata["status"] = explicit_status

            # Load reviewReason if present (e.g., 'plan_review')
            if "reviewReason" in plan:
                metadata["reviewReason"] = plan["reviewReason"]

            # Check for qa_signoff.status == "approved" which means task completed QA
            # This should show as human_review for final merge approval
            qa_signoff = plan.get("qa_signoff") or {}
            if (
                qa_signoff.get("status") == "approved"
                and metadata["status"] == "backlog"
            ):
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "completed"

            # Load archive metadata
            if "archivedAt" in plan:
                metadata["archivedAt"] = plan["archivedAt"]
            if "archivedInVersion" in plan:
                metadata["archivedInVersion"] = plan["archivedInVersion"]

            # Load subtasks - can be at top level or nested in phases
            all_subtasks = []

            # First check for top-level subtasks (legacy format).
            # Tolerate both list shape (canonical) and dict shape
            # (partial-sync artifact from agent_service that maps
            # subtask_id -> {status, notes, ...}).  Without this guard,
            # iterating a dict yields the keys as strings and the loop
            # at the bottom blows up with AttributeError on st.get(...).
            if "subtasks" in plan:
                raw_subtasks = plan["subtasks"]
                if isinstance(raw_subtasks, list):
                    all_subtasks.extend(raw_subtasks)
                elif isinstance(raw_subtasks, dict):
                    for sid, st in raw_subtasks.items():
                        if isinstance(st, dict):
                            st_copy = dict(st)
                            st_copy.setdefault("id", sid)
                            all_subtasks.append(st_copy)

            # Then check for subtasks nested in phases (current format)
            if "phases" in plan:
                for phase in plan["phases"]:
                    if isinstance(phase, dict) and "subtasks" in phase:
                        phase_name = phase.get("name", "")
                        for st in phase["subtasks"]:
                            # Prefix subtask with phase name for clarity
                            st_copy = st.copy() if isinstance(st, dict) else {}
                            if phase_name and "title" not in st_copy:
                                st_copy["title"] = st_copy.get("description", "Subtask")
                            all_subtasks.append(st_copy)

            if all_subtasks:
                metadata["subtasks"] = []
                for i, st in enumerate(all_subtasks):
                    # Build files list from 'file' (single) or 'files'
                    # (array) fields.  Tolerate three shapes the planner
                    # has been observed to emit:
                    #   files: "path/to/x.py"                  (str)
                    #   files: ["a.py", "b.py"]                (list[str])
                    #   files: {"create": ["a.py"], "modify": ["b.py"]}
                    #     (dict — happens when the planner groups files
                    #     by intent; flatten the values into a single
                    #     list of strings)
                    files = []
                    if st.get("file"):
                        files.append(st["file"])
                    raw_files = st.get("files")
                    if isinstance(raw_files, str):
                        files.append(raw_files)
                    elif isinstance(raw_files, list):
                        files.extend(f for f in raw_files if isinstance(f, str))
                    elif isinstance(raw_files, dict):
                        for v in raw_files.values():
                            if isinstance(v, list):
                                files.extend(f for f in v if isinstance(f, str))
                            elif isinstance(v, str):
                                files.append(v)

                    # Build verification from 'verification' or 'verification_method' fields
                    verification = None
                    if st.get("verification"):
                        v = st["verification"]
                        if isinstance(v, dict):
                            verification = SubtaskVerification(
                                type=v.get("type", "command"),
                                run=v.get("run") or v.get("command"),
                                scenario=v.get("scenario"),
                            )
                        elif isinstance(v, str):
                            # Simple string verification becomes a command
                            verification = SubtaskVerification(type="command", run=v)
                    elif st.get("verification_method"):
                        verification = SubtaskVerification(
                            type="command", run=st["verification_method"]
                        )

                    # Dependency edges + timing for the live diagram (#94). The
                    # planner persists these via the implementation_plan Subtask
                    # dataclass; tolerate absence (defaults keep old plans valid).
                    depends_on = st.get("depends_on")
                    if not isinstance(depends_on, list):
                        depends_on = []
                    depends_on = [d for d in depends_on if isinstance(d, str)]

                    # Coerce planner-persisted scalars to the types Subtask
                    # requires. An LLM planner sometimes writes a numeric id
                    # (1.1 as a float) or a non-string title; the strict model
                    # would 500 the whole task list on one such spec (#941/#942).
                    raw_id = st.get("id")
                    subtask_id = str(raw_id) if raw_id is not None else str(i)
                    raw_title = st.get("title") or st.get(
                        "description", f"Subtask {i + 1}"
                    )
                    title = str(raw_title)[:80]
                    raw_desc = st.get("description") or st.get("notes")
                    description = str(raw_desc) if raw_desc is not None else None

                    metadata["subtasks"].append(
                        Subtask(
                            id=subtask_id,
                            title=title,
                            description=description,
                            status=_coerce_subtask_status(st.get("status")),
                            files=files,
                            verification=verification,
                            depends_on=depends_on,
                            service=st.get("service")
                            if isinstance(st.get("service"), str)
                            else None,
                            started_at=st.get("started_at"),
                            completed_at=st.get("completed_at"),
                        )
                    )
        except (json.JSONDecodeError, KeyError, ValidationError, TypeError) as exc:
            # A malformed plan must never 500 the task list or block dispatch
            # (which loads specs through this same helper) — degrade to whatever
            # subtasks parsed cleanly. (#941)
            logger.warning("load_spec_metadata: skipping malformed plan: %s", exc)

    # Check for worktree
    worktree_marker = spec_dir / ".worktree_path"
    if worktree_marker.exists():
        metadata["worktree_path"] = worktree_marker.read_text().strip()
        metadata["branch_name"] = f"aifactory/{spec_dir.name}"

    # #1073: the branch the build actually pushed, recorded at dispatch. Under
    # the kubejob backend there is no .worktree_path marker at all, so the
    # block above never fired and branchName was None for every task built by
    # the deployed backend. This is also the AUTHORITATIVE value: the line
    # above reconstructs the name from a convention, which is a second copy of
    # a rule core.worktree.get_branch_name owns.

    # Load task metadata from requirements.json
    requirements_file = spec_dir / "requirements.json"
    if requirements_file.exists():
        try:
            requirements = json.loads(requirements_file.read_text())
            metadata["task_metadata"] = requirements.get("metadata", {})
        except (json.JSONDecodeError, KeyError):
            metadata["task_metadata"] = {}
    else:
        metadata["task_metadata"] = {}

    # Detect status from subtask progress if not already set
    # If any subtasks are completed but not all done, task is in_progress
    if metadata["status"] == "backlog" and metadata.get("subtasks"):
        subtasks = metadata["subtasks"]
        completed_count = sum(1 for st in subtasks if st.status == "completed")
        in_progress_count = sum(1 for st in subtasks if st.status == "in_progress")
        if completed_count > 0 and completed_count < len(subtasks):
            # Work has been done but not finished
            metadata["status"] = "in_progress"
            metadata["phase"] = "coding"
        elif in_progress_count > 0:
            # Currently working on subtasks
            metadata["status"] = "in_progress"
            metadata["phase"] = "coding"
        elif completed_count == len(subtasks) and len(subtasks) > 0:
            # All subtasks completed - needs review
            metadata["status"] = "human_review"
            metadata["reviewReason"] = "completed"

    # Correctness guard: a phase can log completed even when subtasks failed
    # or the build halted early. Never report a clean completed review state
    # then -- surface that it needs attention so the portal never shows a
    # halted/failed build as Completed. User-set done/completed wins below.
    if explicit_status not in ("done", "completed"):
        _subs = metadata.get("subtasks") or []
        if _subs:
            _n_failed = sum(
                1 for st in _subs if getattr(st, "status", None) == "failed"
            )
            _n_done = sum(
                1 for st in _subs if getattr(st, "status", None) == "completed"
            )
            if _n_failed:
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "errors"
            elif metadata.get("reviewReason") == "completed" and _n_done < len(_subs):
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "incomplete"

    # Final safety: "done"/"completed" always wins over all auto-detection
    # This guards against task_logs or subtask detection overriding user intent
    if explicit_status in ("done", "completed"):
        metadata["status"] = explicit_status

    # Only use file-based status detection if no explicit status was set via kanban
    # AND status wasn't already determined from task_logs.json (coding completed)
    # This allows users to override status via drag-and-drop
    if explicit_status is None and metadata["status"] == "backlog":
        if (spec_dir / "QA_FIX_REQUEST.md").exists():
            metadata["status"] = "human_review"
            metadata["reviewReason"] = "qa_rejected"
        elif (spec_dir / "qa_report.md").exists():
            report = (spec_dir / "qa_report.md").read_text()
            if "PASSED" in report.upper():
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "completed"
            elif "FAILED" in report.upper():
                metadata["status"] = "human_review"
                metadata["reviewReason"] = "qa_rejected"
            else:
                metadata["status"] = "ai_review"  # QA still in progress
        elif metadata["phase"]:
            metadata["status"] = "in_progress"

    # #1069: the plan file exists but does not parse, and nothing else on disk
    # said anything about this task. Report the FAULT rather than falling through
    # to the ``backlog`` default: ``backlog`` means "queued, not started", a claim
    # the code cannot support once it has failed to read the plan — and it also
    # hides the task from the orphan reaper (#1064), which deliberately treats
    # backlog as not-reapable because a queued task has no worker to lose.
    if plan_unreadable and metadata["status"] == "backlog":
        metadata["status"] = PLAN_UNREADABLE
        metadata["reviewReason"] = PLAN_UNREADABLE

    # Control-plane override (Issue #259): the dedicated control store is the
    # authoritative source for board column / status and reviewReason. It is
    # written ONLY by the web-server and is never touched by worktree sync, so
    # a human/system control decision here wins over anything derived above
    # from the agent-written implementation_plan.json / task_logs.json.
    #
    # When the store is absent it falls back (read-time) to any status/
    # reviewReason still living in implementation_plan.json, so pre-#259 specs
    # behave exactly as before.
    control = task_control.read_control(spec_dir)
    if control.get("status"):
        metadata["status"] = control["status"]
        # reviewReason is owned alongside status: take the store's value
        # (which may be absent, meaning "no review reason").
        metadata["reviewReason"] = control.get("reviewReason")

    return metadata


def project_repo(project_data: dict) -> str | None:
    """Best-effort target repo (owner/name) for a project (W5, Factory #218).

    Surfaced on every task so all four portals can show which repo a task runs
    against. Reads the project's git settings, falling back to parsing a clone
    URL. Returns ``None`` when no repo is configured.
    """
    settings = project_data.get("settings") or {}
    for key in ("githubRepo", "gitRepo", "github_repo"):
        value = settings.get(key)
        if value and "/" in str(value):
            return str(value)
    org, name = settings.get("gitOrg"), settings.get("gitProject")
    if org and name:
        return f"{org}/{name}"
    git_url = project_data.get("gitUrl") or project_data.get("git_url")
    if git_url:
        # git@github.com:owner/name.git  or  https://host/owner/name(.git)
        match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", str(git_url))
        if match:
            return match.group(1)
    return None


def spec_to_task(project_id: str, spec_dir: Path) -> Task:
    """Convert a spec directory to a Task model."""
    metadata = load_spec_metadata(spec_dir)

    # #1073: the branch the build pushed. Resolved HERE rather than inside
    # load_spec_metadata because it needs a TRUSTED root: project_id is a
    # registry key (resolve_project_path 404s on anything unknown), whereas the
    # spec_dir that load_spec_metadata receives is a ready-made path that
    # cannot be sanitised after the fact.
    #
    # Under the kubejob backend there is no .worktree_path marker, so the
    # convention-derived branch_name above never fires and this is the only
    # source. The recorded value wins: the other is reconstructed from a
    # convention core.worktree.get_branch_name owns.
    try:
        recorded = recorded_branch(resolve_project_path(project_id), spec_dir.name)
    except Exception:  # noqa: BLE001 - a bad/absent project must not 500 a task list
        recorded = None
    if recorded:
        metadata["branch_name"] = recorded

    # Get timestamps from directory
    stat = spec_dir.stat()

    # Map backend status to frontend-compatible status
    frontend_status = map_backend_status_to_frontend(metadata["status"])

    # Build task metadata if available
    task_metadata = None
    if metadata.get("task_metadata"):
        task_metadata = TaskMetadata(**metadata["task_metadata"])

    return Task(
        id=f"{project_id}:{spec_dir.name}",
        spec_id=spec_dir.name,
        project_id=project_id,
        title=metadata["title"],
        description=metadata["description"],
        status=frontend_status,
        phase=metadata["phase"],
        subtasks=metadata["subtasks"],
        created_at=datetime.fromtimestamp(stat.st_ctime).isoformat(),
        updated_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        worktree_path=metadata["worktree_path"],
        branch_name=metadata["branch_name"],
        metadata=task_metadata,
        review_reason=metadata.get("reviewReason"),
        github_issue=metadata.get("github_issue"),
    )


# durable lifecycle_state -> (frontend status, review_reason); mirrors the
# task_phase.py conventions spec_to_task applies from task_logs.json. A
# ``queued`` lifecycle is intentionally absent so genuinely-queued tasks keep
# their ``backlog`` status.
_DURABLE_STATUS_OVERLAY: dict[str, tuple[TaskStatus, str | None]] = {
    "running": ("in_progress", None),
    "done": ("human_review", "completed"),
    "failed": ("human_review", "errors"),
}


async def overlay_durable_status(tasks: list[Task]) -> None:
    """Correct a stale ``backlog`` status from the authoritative durable store.

    On the packed / out-of-band execution path the agent writes task_logs.json
    into an ephemeral Job workspace that never reaches the control plane, so
    spec_to_task falls back to ``backlog`` for a task that actually ran -- making
    the portals and CFactory show a finished or running task as queued
    (W2, Factory #218). The durable job-state store (RFC-0016) holds the truth,
    so for any task still at the ``backlog`` default we read its lifecycle and
    reproduce the status spec_to_task would have set had task_logs.json reached
    disk.

    Conservative by design: only ``backlog`` tasks are touched, so a real
    spec-dir status is never overridden. Best-effort -- never raises.
    """
    from ..services.job_state_store import store_enabled

    if not store_enabled():
        return
    pending = [task for task in tasks if task.status == "backlog"]
    if not pending:
        return
    try:
        from ..services.agent_service import get_agent_service

        store = get_agent_service()._store()
    except Exception:
        return
    for task in pending:
        try:
            state = await store.get_state(task.id)
        except Exception:
            continue
        lifecycle = (state or {}).get("lifecycle_state")
        mapped = (
            _DURABLE_STATUS_OVERLAY.get(lifecycle)
            if isinstance(lifecycle, str)
            else None
        )
        if mapped is None:
            continue
        task.status, reason = mapped
        if reason is not None:
            task.review_reason = reason


def map_backend_status_to_frontend(backend_status: str) -> str:
    """Map backend task status to frontend-compatible status.

    Backend statuses: backlog, planning, in_progress, review, qa_pending, qa_failed, completed, cancelled
    Frontend statuses: backlog, in_progress, ai_review, human_review, done
    """
    status_mapping = {
        # Backend statuses -> frontend statuses
        "backlog": "backlog",
        "planning": "backlog",  # Planning tasks go in backlog column
        "in_progress": "in_progress",
        "review": "human_review",  # Build ready for review/merge - needs human action
        "qa_pending": "ai_review",
        "qa_failed": "human_review",  # Failed QA needs human attention
        "completed": "human_review",  # Completed tasks need merge approval
        "cancelled": "backlog",  # Cancelled tasks shown in backlog (could be hidden later)
        # #1069: the plan file could not be parsed. A human has to look at the
        # spec dir, so human_review is the honest column — and it must be mapped
        # explicitly, because this function defaults anything unknown to
        # ``backlog`` (which is the bug) and the Kanban board silently DROPS a
        # card whose status is not one of its five columns (which would be
        # worse). The distinct fault survives in review_reason.
        PLAN_UNREADABLE: "human_review",
        # Frontend statuses (pass through when already mapped or set via kanban)
        "ai_review": "ai_review",
        "human_review": "human_review",
        "done": "done",
    }
    return status_mapping.get(backend_status, "backlog")


def get_execution_progress(spec_dir: Path, subtasks: list) -> dict | None:
    """Compute execution progress from task_logs.json and subtasks.

    Returns ExecutionProgress dict or None if not available.
    """
    # Also check worktree for task_logs.json
    project_path = spec_dir.parent.parent  # .aifactory/specs -> project root
    worktree_spec_dir = (
        project_path
        / "worktrees"
        / "tasks"
        / spec_dir.name
        / ".aifactory"
        / "specs"
        / spec_dir.name
    )

    task_logs_file = None
    for check_dir in [worktree_spec_dir, spec_dir]:
        candidate = check_dir / "task_logs.json"
        if candidate.exists():
            task_logs_file = candidate
            break

    if not task_logs_file:
        return None

    try:
        task_logs = json.loads(task_logs_file.read_text())
        phases = task_logs.get("phases", {})

        # Determine current phase from task_logs status
        # Maps task_logs.json phase names to frontend ExecutionPhase values
        phase_map = {
            "planning": "planning",
            "plan_review": "plan_review",
            "coding": "coding",
            "validation": "qa_review",
            "qa_review": "qa_review",
            "qa_fixing": "qa_fixing",
            "complete": "complete",
            "failed": "failed",
        }

        # A weighted per-phase progress calculation was declared here and never
        # written; the loop below reports the ACTIVE phase, not a weighted total.

        current_phase = "idle"
        current_phase_key = None
        started_at = None
        phase_progress = 0

        for log_phase, log_data in phases.items():
            # Get earliest started_at from any phase
            if log_data.get("started_at") and not started_at:
                started_at = log_data["started_at"]
            elif log_data.get("started_at") and started_at:
                # Keep the earliest timestamp
                started_at = min(started_at, log_data["started_at"])

            if log_data.get("status") == "active":
                current_phase = phase_map.get(log_phase, log_phase)
                current_phase_key = log_phase

        # If no active phase, check for terminal states (completed/failed)
        if current_phase == "idle" and phases:
            has_failed = any(p.get("status") == "failed" for p in phases.values())
            has_completed = any(p.get("status") == "completed" for p in phases.values())

            if has_failed:
                current_phase = "failed"
            elif has_completed:
                validation = phases.get("validation", {})
                coding = phases.get("coding", {})
                if (
                    validation.get("status") == "completed"
                    or coding.get("status") == "completed"
                ):
                    current_phase = "complete"

        # Calculate overall progress from subtasks
        completed = sum(1 for s in subtasks if s.status == "completed")
        total = len(subtasks)
        overall_progress = int((completed / total) * 100) if total > 0 else 0

        # Override progress for terminal states
        if current_phase in ("complete", "failed"):
            phase_progress = 100
            overall_progress = 100

        # Calculate phase-specific progress
        if current_phase_key:
            phase_data = phases.get(current_phase_key, {})
            entries = phase_data.get("entries", [])
            # Estimate phase progress based on entries (simple heuristic)
            if entries:
                # Count completed tools vs total activity
                tool_starts = sum(1 for e in entries if e.get("type") == "tool_start")
                tool_ends = sum(1 for e in entries if e.get("type") == "tool_end")
                if tool_starts > 0:
                    phase_progress = min(100, int((tool_ends / tool_starts) * 100))
                else:
                    phase_progress = 50  # Activity detected but no tools tracked
            else:
                phase_progress = 10  # Phase started but no entries yet

        # Find current subtask
        current_subtask = None
        for s in subtasks:
            if s.status == "in_progress":
                current_subtask = s.title
                break

        # Generate sequence number from file modification time for stale update detection
        sequence_number = int(task_logs_file.stat().st_mtime * 1000)

        return {
            "phase": current_phase,
            "phaseProgress": phase_progress,
            "overallProgress": overall_progress,
            "currentSubtask": current_subtask,
            "message": f"{completed}/{total} subtasks completed",
            "startedAt": started_at,
            "sequenceNumber": sequence_number,
        }
    except (json.JSONDecodeError, Exception):
        return None


def task_to_dict(task: Task) -> dict:
    """Convert a Task model to a dict with camelCase keys for frontend."""
    # Get execution progress and archive metadata if task has a spec directory
    execution_progress = None
    archive_metadata = {}
    specs_path = None
    if task.spec_id:
        # Try to find spec dir for this task
        projects = load_projects()
        if task.project_id in projects:
            project_path = Path(projects[task.project_id]["path"])
            spec_dir = project_path / ".aifactory" / "specs" / task.spec_id
            if spec_dir.exists():
                specs_path = str(spec_dir)  # Store path for frontend Files tab
                execution_progress = get_execution_progress(spec_dir, task.subtasks)
                # Load archive metadata from plan file
                plan_file = spec_dir / "implementation_plan.json"
                if plan_file.exists():
                    try:
                        plan = json.loads(plan_file.read_text())
                        if "archivedAt" in plan:
                            archive_metadata["archivedAt"] = plan["archivedAt"]
                        if "archivedInVersion" in plan:
                            archive_metadata["archivedInVersion"] = plan[
                                "archivedInVersion"
                            ]
                    except json.JSONDecodeError:
                        pass

    result = {
        "id": task.id,
        "specId": task.spec_id,
        "projectId": task.project_id,
        "title": task.title,
        "description": task.description,
        "status": map_backend_status_to_frontend(task.status),
        "phase": task.phase,
        # RFC-0001 correlation: surface the upstream GitHub issue so the cockpit
        # threads this build with its PFactory plan + TFactory test.
        "githubIssueNumber": task.github_issue,
        # W5 (Factory #218): target repo (owner/name) for cross-portal tracking.
        "repo": task.repo,
        "subtasks": [
            {
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "status": s.status,
                "files": s.files,
                "verification": {
                    "type": s.verification.type,
                    "run": s.verification.run,
                    "scenario": s.verification.scenario,
                }
                if s.verification
                else None,
                # Dependency graph + timing for the cockpit's live diagram (#94).
                "depends_on": getattr(s, "depends_on", []),
                "service": getattr(s, "service", None),
                "started_at": getattr(s, "started_at", None),
                "completed_at": getattr(s, "completed_at", None),
            }
            for s in task.subtasks
        ],
        "logs": [],  # Required by frontend Task interface
        "createdAt": task.created_at,
        "updatedAt": task.updated_at,
        "worktreePath": task.worktree_path,
        "branchName": task.branch_name,
        "reviewReason": task.review_reason,
        "specsPath": specs_path,  # Path to spec directory for Files tab
    }

    if execution_progress:
        result["executionProgress"] = execution_progress

    # Include task metadata (settings from requirements.json)
    metadata_payload = (
        task.metadata.model_dump(exclude_none=True) if task.metadata else {}
    )
    if archive_metadata:
        metadata_payload.update(archive_metadata)  # Add archive info if any
    if metadata_payload:
        result["metadata"] = metadata_payload

    return result


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _pfactory_priority_rank(spec_dir: Path) -> int:
    """PFactory priority sort rank for a spec (p0 → first); 99 when not a
    prioritised PFactory spec. Best-effort: any failure sorts the task last.
    See epic #327 / #331.
    """
    import sys

    backend_path = Path(__file__).parent.parent.parent.parent / "backend"
    if str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))
    try:
        from pfactory.routing import priority_rank
        from pfactory.taxonomy import classify_requirements

        req_file = spec_dir / "requirements.json"
        if req_file.exists():
            req = json.loads(req_file.read_text())
            return priority_rank(classify_requirements(req).priority)
    except (json.JSONDecodeError, OSError, ImportError):
        pass
    return 99
