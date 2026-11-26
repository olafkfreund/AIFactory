"""Confirm a runner's needs-input pause against the dispatched attempt."""

from __future__ import annotations

import json
import logging
import secrets
from contextlib import suppress
from pathlib import Path

from agents.questions import (
    QID_RE,
    RUN_ID_RE,
    TASK_QUESTION_LIMIT,
    create_question,
    validate_question,
)
from core.file_utils import atomic_write
from factory_common.logsafe import sanitize_log

logger = logging.getLogger(__name__)
_ATTEMPT_FILE = ".question_attempt"


def _question_files(spec_dir: Path) -> set[Path]:
    try:
        return {
            path
            for path in (spec_dir / "questions").iterdir()
            if path.suffix == ".json" and QID_RE.fullmatch(path.stem) and path.is_file()
        }
    except FileNotFoundError:
        return set()


def start_attempt(source_spec_dir: Path) -> dict[str, str]:
    """Record the control plane's attempt before admitting runner output."""
    attempt = secrets.token_hex(16)
    count = len(_question_files(source_spec_dir))
    # A dispatch without a source spec can run, but cannot confirm a pause.
    if source_spec_dir.is_dir():
        with atomic_write(source_spec_dir / _ATTEMPT_FILE) as stream:
            stream.write(attempt)
    return {
        "AIFACTORY_ATTEMPT_ID": attempt,
        "AIFACTORY_TASK_QUESTION_COUNT": str(count),
    }


def _read_attempt(spec_dir: Path) -> str:
    attempt = (spec_dir / _ATTEMPT_FILE).read_text(encoding="utf-8")
    if not RUN_ID_RE.fullmatch(attempt):
        raise ValueError("invalid question attempt")
    return attempt


def local_pause_claimed(spec_dir: Path) -> bool:
    try:
        plan = json.loads(
            (spec_dir / "implementation_plan.json").read_text(encoding="utf-8")
        )
        return (
            isinstance(plan, dict)
            and plan.get("status") == "human_review"
            and plan.get("reviewReason") == "needs_input"
        )
    except (OSError, ValueError, RecursionError):
        return False


def confirm_local(spec_dir: Path) -> dict | None:
    """Find an open question published to the source directory by this attempt."""
    try:
        attempt = _read_attempt(spec_dir)
        files = _question_files(spec_dir)
        if len(files) <= TASK_QUESTION_LIMIT:
            for path in sorted(files):
                with suppress(ValueError, RecursionError):
                    record = validate_question(
                        json.loads(path.read_text(encoding="utf-8")), run_id=attempt
                    )
                    if record["id"] == path.stem:
                        return record
    except Exception:  # noqa: BLE001 - any uncertainty refuses confirmation
        return None
    return None


def confirm_kubejob(spec_dir: Path, spec_id: str) -> dict | None:
    """Validate and persist this Job's open questions before accepting the pause."""
    created: list[Path] = []
    try:
        from core.workspace_fetch import fetch_questions  # noqa: PLC0415

        attempt = _read_attempt(spec_dir)
        records = [
            validate_question(record, run_id=attempt)
            for record in fetch_questions(spec_id, attempt)
        ]
        destinations = {
            spec_dir / "questions" / f"{record['id']}.json" for record in records
        }
        if (
            not records
            or len(_question_files(spec_dir) | destinations) > TASK_QUESTION_LIMIT
        ):
            return None
        for record in records:
            if not create_question(spec_dir, record, created=created):
                raise ValueError("question id already exists with different content")
    except Exception:  # noqa: BLE001 - roll back only files created by this confirmation
        for path in created:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        return None
    return records[0]


def on_question_open(task_id: str) -> None:
    logger.debug("Confirmed a needs-input pause for %s", sanitize_log(task_id))
