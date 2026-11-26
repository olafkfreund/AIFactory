"""Durable questions from coder sessions."""

import fcntl
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUN_FILE = ".question_run_id"
QID_RE = re.compile(r"^q-[0-9a-f]{12}$")


def valid_qid(qid: str) -> bool:
    return isinstance(qid, str) and QID_RE.fullmatch(qid) is not None


class QuestionBudgetExhausted(ValueError):
    """The run or task has used its question allowance."""


def start_question_run(spec_dir: Path) -> str:
    """Give all coder sessions in one build invocation the same run ID."""
    run_id = uuid.uuid4().hex
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / RUN_FILE).write_text(run_id, encoding="utf-8")
    return run_id


def question_run_id(spec_dir: Path) -> str:
    return (spec_dir / RUN_FILE).read_text(encoding="utf-8").strip()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _lock(spec_dir: Path):
    directory = spec_dir / "questions"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_question(
    spec_dir: Path,
    *,
    question: str,
    run_id: str,
    asked_by_model: str,
    choices: list[str] | None = None,
    needs_approval: bool = False,
    context: str = "",
    default: str | None = None,
    timeout_minutes: int = 30,
    budget_spec_dir: Path | None = None,
    prior_task_count: int = 0,
) -> dict[str, Any]:
    """Write one question and charge both budgets under the task lock."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question is required")
    question = question.strip()
    if len(question) > 2000:
        raise ValueError("question must be at most 2000 characters")
    if not isinstance(needs_approval, bool):
        raise ValueError("needs_approval must be a boolean")
    if not isinstance(context, str) or (
        default is not None and not isinstance(default, str)
    ):
        raise ValueError("context and default must be strings")
    if len(context) > 4000:
        raise ValueError("context must be at most 4000 characters")
    if default is not None:
        default = default.strip()
        if len(default) > 500:
            raise ValueError("default must be at most 500 characters")
    if not isinstance(run_id, str) or not run_id or not isinstance(asked_by_model, str):
        raise ValueError("run_id and asked_by_model are required")
    if choices is not None and not isinstance(choices, list):
        raise ValueError("choices must contain 2-6 distinct nonempty strings")
    if choices is not None:
        choices = [
            choice.strip() if isinstance(choice, str) else choice for choice in choices
        ]
    if choices is not None and (
        not 2 <= len(choices) <= 6
        or any(not isinstance(choice, str) or not choice.strip() for choice in choices)
        or len(set(choices)) != len(choices)
    ):
        raise ValueError("choices must contain 2-6 distinct nonempty strings")
    if choices is not None and any(len(choice) > 200 for choice in choices):
        raise ValueError("each choice must be at most 200 characters")
    if (
        isinstance(timeout_minutes, bool)
        or not isinstance(timeout_minutes, int)
        or not 1 <= timeout_minutes <= 1440
    ):
        raise ValueError("timeout_minutes must be between 1 and 1440")
    budget_spec_dir = budget_spec_dir or spec_dir
    with _lock(budget_spec_dir) as budget_dir:
        budget_path = budget_dir / ".budget.json"
        budget = (
            json.loads(budget_path.read_text())
            if budget_path.exists()
            else {"total": 0, "runs": {}}
        )
        if (
            max(budget["total"], prior_task_count + budget["runs"].get(run_id, 0)) >= 10
            or budget["runs"].get(run_id, 0) >= 3
        ):
            raise QuestionBudgetExhausted(
                "question budget exhausted: proceed on your best reading or stop"
            )
        qid = f"q-{uuid.uuid4().hex[:12]}"
        while (spec_dir / "questions" / f"{qid}.json").exists():
            qid = f"q-{uuid.uuid4().hex[:12]}"
        record = {
            "id": qid,
            "question": question,
            "choices": choices,
            "needs_approval": needs_approval,
            "context": context,
            "default": default,
            "timeout_minutes": timeout_minutes,
            "status": "open",
            "asked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "asked_by_model": asked_by_model,
            "run_id": run_id,
        }
        budget["total"] += 1
        budget["runs"][run_id] = budget["runs"].get(run_id, 0) + 1
        _atomic_json(budget_path, budget)
        _atomic_json(spec_dir / "questions" / f"{qid}.json", record)
        return record


def read_question(spec_dir: Path, qid: str) -> dict[str, Any]:
    if not valid_qid(qid):
        raise ValueError("invalid question id")
    return json.loads((spec_dir / "questions" / f"{qid}.json").read_text())


def list_questions(spec_dir: Path) -> list[dict[str, Any]]:
    directory = spec_dir / "questions"
    if not directory.exists():
        return []
    return [
        json.loads(path.read_text())
        for path in sorted(directory.glob("*.json"))
        if not path.name.startswith(".")
    ]


def update_question(
    spec_dir: Path, qid: str, *, expected_status: str, changes: dict[str, Any]
) -> dict[str, Any] | None:
    """Compare and set one question; return None when its status moved."""
    if not valid_qid(qid):
        raise ValueError("invalid question id")
    with _lock(spec_dir):
        record = read_question(spec_dir, qid)
        if record["status"] != expected_status:
            return None
        record.update(changes)
        _atomic_json(spec_dir / "questions" / f"{qid}.json", record)
        return record
