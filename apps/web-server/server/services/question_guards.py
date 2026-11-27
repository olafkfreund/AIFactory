"""Control-plane guards for tasks paused on questions."""

import json
from datetime import UTC, datetime
from pathlib import Path

from agents.questions import QID_RE, update_question
from core.file_utils import write_json_atomic
from fastapi import HTTPException

from .task_control import read_control, write_control

APPROVAL_PENDING = "approval question pending: answer it in the portal"


def paused_reason(control: dict[str, object]) -> str | None:
    reason = control.get("reviewReason")
    if reason in ("needs_input", "approval_rejected", "plan_review"):
        return str(reason)
    return None


def _rejected_questions(spec_dir: Path, *, allow_rejected: bool) -> list[str]:
    directory = spec_dir / "questions"
    if directory.is_symlink():
        raise ValueError("invalid questions directory")
    try:
        paths = list(directory.iterdir())
    except FileNotFoundError:
        return []

    rejected = []
    for path in paths:
        if path.suffix != ".json" or not QID_RE.fullmatch(path.stem):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid question file")
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("needs_approval"), bool)
            or record.get("status") not in ("open", "expired", "answered", "rejected")
        ):
            raise ValueError("invalid question record")
        if record["needs_approval"] and record["status"] in ("open", "expired"):
            raise ValueError("pending approval question")
        if record["status"] == "rejected" and "closed_at" not in record:
            if not allow_rejected:
                raise ValueError("unclosed approval rejection")
            rejected.append(path.stem)
    return rejected


def approval_rejected(spec_dir: Path) -> bool:
    control = read_control(spec_dir)
    return (
        "_migratedFromPlan" not in control
        and control.get("reviewReason") == "approval_rejected"
    )


def approval_blocked(spec_dir: Path, *, retry_rejected: bool = False) -> bool:
    """Check the authored spec, allowing rejection only on a confirmed retry."""
    try:
        rejected = approval_rejected(spec_dir)
        if rejected and not retry_rejected:
            return True
        _rejected_questions(spec_dir, allow_rejected=rejected and retry_rejected)
    except (OSError, ValueError, RecursionError):
        return True
    return False


def require_approval_clear(spec_dir: Path, *, retry_rejected: bool = False) -> None:
    if approval_blocked(spec_dir, retry_rejected=retry_rejected):
        raise HTTPException(status_code=409, detail=APPROVAL_PENDING)


def _clear_review_marker(spec_dir: Path) -> None:
    path = spec_dir / "review_state.json"
    if path.is_symlink():
        raise ValueError("invalid review state file")
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    if isinstance(review, dict) and review.get("reason") in (
        "needs_input",
        "approval_rejected",
    ):
        review.pop("reason")
        review.pop("qid", None)
        write_json_atomic(path, review)


def close_approval_rejection(spec_dir: Path) -> None:
    """Close rejected questions immediately before the validated retry runs."""
    try:
        rejected = approval_rejected(spec_dir)
        qids = _rejected_questions(spec_dir, allow_rejected=rejected)
        if not rejected:
            return
        closed_at = (
            datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        for qid in qids:
            if (
                update_question(
                    spec_dir,
                    qid,
                    expected_status="rejected",
                    changes={"closed_at": closed_at},
                )
                is None
            ):
                raise ValueError("approval question changed during retry")
        _clear_review_marker(spec_dir)
        write_control(spec_dir, clear_review_reason=True, updated_by="web_user")
    except (OSError, ValueError, KeyError, RecursionError) as exc:
        raise HTTPException(status_code=409, detail=APPROVAL_PENDING) from exc


def guard_task_status(spec_dir: Path, new_status: str | None) -> None:
    if new_status is None:
        return
    control = read_control(spec_dir)
    reason = control.get("reviewReason")
    if (
        "_migratedFromPlan" not in control
        and reason in ("needs_input", "approval_rejected")
        and not new_status == control.get("status") == "human_review"
    ):
        raise HTTPException(status_code=409, detail=f"task is paused: {reason}")
