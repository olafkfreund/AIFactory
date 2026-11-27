"""Durable questions from coder sessions."""

import fcntl
import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUN_FILE = ".question_run_id"
QID_RE = re.compile(r"^q-[0-9a-f]{12}$")
RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
QID_HEX_LENGTH = 12
QUESTION_MAX_LENGTH = 2000
CHOICE_MIN_COUNT = 2
CHOICE_MAX_COUNT = 6
CHOICE_MAX_LENGTH = 200
CONTEXT_MAX_LENGTH = 4000
DEFAULT_MAX_LENGTH = 500
MODEL_MAX_LENGTH = 200
TIMEOUT_MAX_MINUTES = 1440
RUN_QUESTION_LIMIT = 3
TASK_QUESTION_LIMIT = 10
ASKED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
QUESTION_FIELDS = {
    "id",
    "question",
    "choices",
    "needs_approval",
    "context",
    "default",
    "timeout_minutes",
    "status",
    "asked_at",
    "asked_by_model",
    "run_id",
}


def valid_qid(qid: str) -> bool:
    return isinstance(qid, str) and QID_RE.fullmatch(qid) is not None


class QuestionBudgetExhausted(ValueError):  # noqa: N818 - tool budget signal
    """The run or task has used its question allowance."""


class QuestionPause(Exception):  # noqa: N818 - runner control-flow signal
    """A parallel worker has a question for the main runner to checkpoint."""


class QuestionCheckpointError(RuntimeError):
    """A question cannot be safely checkpointed or transferred."""


def start_question_run(spec_dir: Path) -> str:
    """Give all coder sessions in one build invocation the same run ID."""
    attempt_id = os.environ.get("AIFACTORY_ATTEMPT_ID", "")
    run_id = attempt_id if RUN_ID_RE.fullmatch(attempt_id) else uuid.uuid4().hex
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / RUN_FILE).write_text(run_id, encoding="utf-8")
    return run_id


def question_run_id(spec_dir: Path) -> str:
    return (spec_dir / RUN_FILE).read_text(encoding="utf-8").strip()


@contextmanager
def _staged_json(path: Path, value: dict[str, Any]) -> Iterator[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield Path(temporary)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    with _staged_json(path, value) as temporary:
        temporary.replace(path)


def create_question(
    spec_dir: Path, record: dict[str, Any], *, created: list[Path] | None = None
) -> bool:
    """Publish once, optionally recording files owned by this transfer for rollback."""
    validate_question(record)
    dest = spec_dir / "questions" / f"{record['id']}.json"
    with _staged_json(dest, record) as temporary:
        try:
            os.link(temporary, dest)
        except FileExistsError:
            try:
                existing = validate_question(
                    json.loads(dest.read_text(encoding="utf-8"))
                )
                return existing == record
            except ValueError:
                return False
        if created is not None:
            created.append(dest)
    return True


@contextmanager
def _lock(spec_dir: Path) -> Iterator[Path]:
    directory = spec_dir / "questions"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _validate_choices(choices: list[str] | None) -> None:
    if choices is None:
        return
    if (
        not isinstance(choices, list)
        or not CHOICE_MIN_COUNT <= len(choices) <= CHOICE_MAX_COUNT
        or any(not isinstance(choice, str) or not choice.strip() for choice in choices)
        or len(set(choices)) != len(choices)
    ):
        raise ValueError(
            f"choices must contain {CHOICE_MIN_COUNT}-{CHOICE_MAX_COUNT} distinct nonempty strings"
        )
    if any(len(choice) > CHOICE_MAX_LENGTH for choice in choices):
        raise ValueError(f"each choice must be at most {CHOICE_MAX_LENGTH} characters")


def _validate_arguments(record: dict[str, Any]) -> None:
    question = record["question"]
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question is required")
    if len(question) > QUESTION_MAX_LENGTH:
        raise ValueError(f"question must be at most {QUESTION_MAX_LENGTH} characters")
    if not isinstance(record["needs_approval"], bool):
        raise ValueError("needs_approval must be a boolean")
    for field, limit in (
        ("context", CONTEXT_MAX_LENGTH),
        ("default", DEFAULT_MAX_LENGTH),
    ):
        value = record[field]
        if field == "default" and value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("context and default must be strings")
        if len(value) > limit:
            raise ValueError(f"{field} must be at most {limit} characters")
    model = record["asked_by_model"]
    if not isinstance(model, str) or len(model) > MODEL_MAX_LENGTH:
        raise ValueError(
            f"asked_by_model must be a string of at most {MODEL_MAX_LENGTH} characters"
        )
    _validate_choices(record["choices"])
    timeout = record["timeout_minutes"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= TIMEOUT_MAX_MINUTES
    ):
        raise ValueError(f"timeout_minutes must be between 1 and {TIMEOUT_MAX_MINUTES}")


def validate_question(record: object, *, run_id: str | None = None) -> dict[str, Any]:
    """Validate an open question at a transfer boundary."""
    if not isinstance(record, dict) or record.keys() != QUESTION_FIELDS:
        raise ValueError("invalid question fields")
    if not valid_qid(record["id"]) or record["status"] != "open":
        raise ValueError("invalid question id or status")
    attempt = record["run_id"]
    if not isinstance(attempt, str) or not RUN_ID_RE.fullmatch(attempt):
        raise ValueError("invalid question run id")
    if run_id is not None and attempt != run_id:
        raise ValueError("question belongs to a different run")
    asked_at = record["asked_at"]
    if (
        not isinstance(asked_at, str)
        or datetime.strptime(asked_at, ASKED_AT_FORMAT)
        .replace(tzinfo=UTC)
        .strftime(ASKED_AT_FORMAT)
        != asked_at
    ):
        raise ValueError("invalid question timestamp")
    _validate_arguments(record)
    return record


def write_question(  # noqa: PLR0913 - explicit tool inputs and budget context
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
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required")
    if isinstance(choices, list):
        choices = [
            choice.strip() if isinstance(choice, str) else choice for choice in choices
        ]
    record = {
        "question": question.strip() if isinstance(question, str) else question,
        "choices": choices,
        "needs_approval": needs_approval,
        "context": context,
        "default": default.strip() if isinstance(default, str) else default,
        "timeout_minutes": timeout_minutes,
        "status": "open",
        "asked_at": datetime.now(UTC).strftime(ASKED_AT_FORMAT),
        "asked_by_model": asked_by_model,
        "run_id": run_id,
    }
    _validate_arguments(record)
    budget_spec_dir = budget_spec_dir or spec_dir
    with _lock(budget_spec_dir) as budget_dir:
        budget_path = budget_dir / ".budget.json"
        budget: dict[str, Any] = (
            json.loads(budget_path.read_text())
            if budget_path.exists()
            else {"total": 0, "runs": {}}
        )
        if (
            max(budget["total"], prior_task_count + budget["runs"].get(run_id, 0))
            >= TASK_QUESTION_LIMIT
            or budget["runs"].get(run_id, 0) >= RUN_QUESTION_LIMIT
        ):
            raise QuestionBudgetExhausted(
                "question budget exhausted: proceed on your best reading or stop"
            )
        qid = f"q-{uuid.uuid4().hex[:QID_HEX_LENGTH]}"
        while (spec_dir / "questions" / f"{qid}.json").exists():
            qid = f"q-{uuid.uuid4().hex[:QID_HEX_LENGTH]}"
        record["id"] = qid
        budget["total"] += 1
        budget["runs"][run_id] = budget["runs"].get(run_id, 0) + 1
        _atomic_json(budget_path, budget)
        _atomic_json(spec_dir / "questions" / f"{qid}.json", record)
        return record


def read_question(spec_dir: Path, qid: str) -> dict[str, Any]:
    if not valid_qid(qid):
        raise ValueError("invalid question id")
    record: dict[str, Any] = json.loads(
        (spec_dir / "questions" / f"{qid}.json").read_text()
    )
    return record


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
