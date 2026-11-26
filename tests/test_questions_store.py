"""Question storage and budget checks; no services required."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from agents.questions import (
    QuestionBudgetExhausted,
    list_questions,
    read_question,
    update_question,
    valid_qid,
    write_question,
)


def _ask(spec_dir, run_id="run-a", **kwargs):
    return write_question(
        spec_dir,
        question=kwargs.pop("question", "Which format?"),
        run_id=run_id,
        asked_by_model="claude",
        **kwargs,
    )


def test_question_roundtrip_and_compare_and_set(tmp_path):
    record = _ask(tmp_path, choices=["JSON", "CSV"], needs_approval=True)
    assert re.fullmatch(r"q-[0-9a-f]{12}", record["id"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", record["asked_at"])
    assert read_question(tmp_path, record["id"]) == record
    assert list_questions(tmp_path) == [record]
    assert (
        update_question(
            tmp_path,
            record["id"],
            expected_status="open",
            changes={"status": "answered"},
        )["status"]
        == "answered"
    )
    assert (
        update_question(
            tmp_path,
            record["id"],
            expected_status="open",
            changes={"status": "rejected"},
        )
        is None
    )


@pytest.mark.parametrize(
    "qid,expected",
    [
        ("q-3f9a1c7e20b4", True),
        ("../x", False),
        ("", False),
        ("a" * 32, False),
        ("q-3F9A1C7E20B4", False),
    ],
)
def test_valid_question_ids(tmp_path, qid, expected):
    assert valid_qid(qid) is expected
    if not expected:
        with pytest.raises(ValueError, match="^invalid question id$"):
            read_question(tmp_path, qid)
        with pytest.raises(ValueError, match="^invalid question id$"):
            update_question(tmp_path, qid, expected_status="open", changes={})


def test_question_id_collision_retries(tmp_path, monkeypatch):
    from agents import questions

    ids = iter(["a" * 32, "a" * 32, "b" * 32])
    monkeypatch.setattr(
        questions.uuid, "uuid4", lambda: type("UUID", (), {"hex": next(ids)})()
    )
    assert _ask(tmp_path)["id"] == "q-aaaaaaaaaaaa"
    assert _ask(tmp_path)["id"] == "q-bbbbbbbbbbbb"
    assert len(list_questions(tmp_path)) == 2


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"question": "x" * 2001}, "question.*2000"),
        ({"context": "x" * 4001}, "context.*4000"),
        ({"default": "x" * 501}, "default.*500"),
        ({"choices": ["a", "x" * 201]}, "choice.*200"),
        ({"timeout_minutes": 0}, "timeout_minutes"),
        ({"timeout_minutes": 1441}, "timeout_minutes"),
        ({"timeout_minutes": True}, "timeout_minutes"),
    ],
)
def test_question_limits(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        _ask(tmp_path, **kwargs)
    assert list_questions(tmp_path) == []


def test_question_boundaries_and_stripping(tmp_path):
    record = _ask(tmp_path, choices=[" a ", " b "], default=" a ", timeout_minutes=1440)
    assert record["choices"] == ["a", "b"]
    assert record["default"] == "a"
    assert record["timeout_minutes"] == 1440


def test_prior_task_count_limits_task_within_run(tmp_path):
    _ask(tmp_path, prior_task_count=9)
    with pytest.raises(QuestionBudgetExhausted, match="question budget exhausted"):
        _ask(tmp_path, prior_task_count=9)
    budget = json.loads((tmp_path / "questions" / ".budget.json").read_text())
    assert budget["total"] == 1


def test_run_and_task_budgets(tmp_path):
    for _ in range(3):
        _ask(tmp_path)
    with pytest.raises(QuestionBudgetExhausted, match="question budget exhausted"):
        _ask(tmp_path)
    for run in ("b", "c"):
        for _ in range(3):
            _ask(tmp_path, run_id=run)
    _ask(tmp_path, run_id="d")
    with pytest.raises(QuestionBudgetExhausted, match="question budget exhausted"):
        _ask(tmp_path, run_id="d")


@pytest.mark.parametrize("choices", [["one"], ["a"] * 2, ["a", ""], list("abcdefg")])
def test_invalid_choices(tmp_path, choices):
    with pytest.raises(ValueError, match="choices"):
        _ask(tmp_path, choices=choices)
    assert list_questions(tmp_path) == []


def test_parallel_workers_share_budget_but_write_to_own_specs(tmp_path):
    parent = tmp_path / "parent"

    def ask(index):
        try:
            return _ask(tmp_path / f"worker-{index}", budget_spec_dir=parent)
        except QuestionBudgetExhausted:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(ask, range(4)))
    assert sum(isinstance(record, dict) for record in records) == 3
    assert sum(len(list_questions(tmp_path / f"worker-{i}")) for i in range(4)) == 3
