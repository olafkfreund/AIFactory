"""Question storage and budget checks; no services required."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from agents.questions import (
    QuestionBudgetExhausted,
    list_questions,
    read_question,
    update_question,
    write_question,
)


def _ask(spec_dir, run_id="run-a", **kwargs):
    return write_question(
        spec_dir,
        question="Which format?",
        run_id=run_id,
        asked_by_model="claude",
        **kwargs,
    )


def test_question_roundtrip_and_compare_and_set(tmp_path):
    record = _ask(tmp_path, choices=["JSON", "CSV"], needs_approval=True)
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
