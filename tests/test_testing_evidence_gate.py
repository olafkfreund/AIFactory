#!/usr/bin/env python3
"""The testing subtask cannot be completed with a design document (#1176).

Fixtures are the live shape from ``olafkfreund/aifactory-demo`` specs 101 and
108: a ``service == "testing"`` subtask whose only declared deliverable is
``docs/plans/<id>-testing-strategy.md``, beside acceptance criteria about test
lanes being runnable.

The first two tests are the controls: they reproduce the two independent reasons
#851 does not already cover this subtask, rather than asserting it in prose. The
rest drive the real serial completion tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from agents.completion_gate import completion_refusal  # noqa: E402
from agents.test_evidence import read_test_evidence, record_test_run  # noqa: E402
from agents.testing_evidence import testing_deliverable_gap  # noqa: E402
from agents.tools_pkg.tools.subtask import (  # noqa: E402
    apply_subtask_status_update,
)

STRATEGY_DOC = "docs/plans/030-vat-quote-endpoint-testing-strategy.md"
TEST_FILE = "tests/test_vat_quote.py"

CRITERIA = [
    "Unit, integration, and e2e lanes are scaffolded and runnable.",
    "Every plan acceptance criterion maps to at least one passing test.",
]

# Verbatim shape from the live implementation_plan.json of both runs.
PROSE_SUBTASK: dict[str, Any] = {
    "id": "TEST",
    "description": "Test the VAT quote endpoint",
    "service": "testing",
    "files_to_create": [STRATEGY_DOC],
    "acceptance_criteria": CRITERIA,
}
# What PFactory#461 will emit once the testing child names real test files.
REAL_SUBTASK: dict[str, Any] = {
    "id": "TEST",
    "description": "Test the VAT quote endpoint",
    "service": "testing",
    "files_to_create": [TEST_FILE, STRATEGY_DOC],
    "acceptance_criteria": CRITERIA,
}

RAN_GREEN: dict[str, Any] = {
    "ran": True,
    "last_failed": False,
    "runs": 1,
    "last_command": "pytest -q",
}


def _project(tmp_path: Path, *, with_test_file: bool) -> Path:
    root = tmp_path / "project"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / STRATEGY_DOC).write_text("# Testing Strategy\n")
    if with_test_file:
        (root / "tests").mkdir()
        (root / TEST_FILE).write_text("def test_quote():\n    assert True\n")
    return root


def _spec_dir(tmp_path: Path, subtask: dict[str, Any]) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "implementation_plan.json").write_text(
        json.dumps(
            {"phases": [{"phase": 1, "name": "Implementation", "subtasks": [subtask]}]}
        )
    )
    return spec


def test_851_does_not_even_recognise_the_live_testing_subtask(tmp_path: Path):
    """First control. #851 decides "is this a verification subtask" from title /
    description text, and the live TEST subtask's description ("Test the VAT
    quote endpoint"; in run 101 literally "No description") matches none of its
    keywords. So the gate the fleet leans on never fires on the plan's OWN
    testing subtask. The #1176 gate keys on `service`, the contract field, which
    is why it does."""
    project = _project(tmp_path, with_test_file=False)
    from agents.test_evidence import is_verification_subtask

    assert is_verification_subtask(PROSE_SUBTASK) is False
    assert PROSE_SUBTASK["service"] == "testing"
    assert testing_deliverable_gap(PROSE_SUBTASK, project) is not None


def test_851_is_satisfied_by_an_earlier_subtasks_run(tmp_path: Path):
    """Second control (#1187), independent of the first. Even when the wording
    DOES make it a verification subtask, #851 asks "did a test command run this
    build" and the serial engine reads the WHOLE build's evidence — so an earlier
    subtask's green pytest satisfies it for a subtask that shipped only prose.
    Both holes are why this gate is keyed on declared deliverables rather than on
    run evidence."""
    worded = {**PROSE_SUBTASK, "description": "Set up the unit tests for rounding"}
    spec = _spec_dir(tmp_path, worded)
    project = _project(tmp_path, with_test_file=False)

    # an EARLIER subtask in this build ran the suite
    record_test_run(spec, "pytest -q", "collected 3 items ... 3 passed")
    evidence = read_test_evidence(spec)
    assert evidence["ran"] is True

    from agents.test_evidence import deliverable_evidence_gap, is_verification_subtask

    assert is_verification_subtask(worded) is True
    assert deliverable_evidence_gap(worded, project) is None  # #1111 silent too

    # The #1176 gate is what refuses it.
    assert testing_deliverable_gap(worded, project) is not None


def test_refuses_the_documenting_coder(tmp_path: Path):
    spec = _spec_dir(tmp_path, PROSE_SUBTASK)
    project = _project(tmp_path, with_test_file=False)
    refusal = completion_refusal(PROSE_SUBTASK, project, RAN_GREEN)
    assert refusal is not None
    assert "every file it declares is documentation" in refusal
    assert STRATEGY_DOC in refusal
    assert "'failed'" in refusal  # the honest alternative is always offered
    assert spec.exists()


@pytest.mark.asyncio
async def test_update_subtask_status_refuses_the_documenting_coder(tmp_path: Path):
    """The real serial tool, not the check in isolation: the plan must be left
    untouched when the gate refuses."""
    spec = _spec_dir(tmp_path, PROSE_SUBTASK)
    project = _project(tmp_path, with_test_file=False)

    out = await apply_subtask_status_update(
        spec, "TEST", "completed", project_dir=project
    )
    assert "every file it declares is documentation" in out["content"][0]["text"]

    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["phases"][0]["subtasks"][0].get("status", "pending") != "completed"


@pytest.mark.asyncio
async def test_failing_a_subtask_is_never_blocked(tmp_path: Path):
    """RFC-0006: honesty must always be available."""
    spec = _spec_dir(tmp_path, PROSE_SUBTASK)
    project = _project(tmp_path, with_test_file=False)
    await apply_subtask_status_update(spec, "TEST", "failed", project_dir=project)
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["phases"][0]["subtasks"][0]["status"] == "failed"


def test_promised_tests_that_do_not_exist_are_refused(tmp_path: Path):
    """Post-#461 shape: the subtask names a real test file and the coder still
    wrote only the document."""
    project = _project(tmp_path, with_test_file=False)
    refusal = completion_refusal(REAL_SUBTASK, project, RAN_GREEN)
    assert refusal is not None
    assert TEST_FILE in refusal
    assert "do not exist" in refusal


def test_real_tests_pass_with_no_edit(tmp_path: Path):
    """The property that keeps a gate from being routed around: correct work is
    allowed through untouched. This is what the gate looks like once #461 lands."""
    project = _project(tmp_path, with_test_file=True)
    assert completion_refusal(REAL_SUBTASK, project, RAN_GREEN) is None


def test_ordinary_subtasks_are_untouched(tmp_path: Path):
    """Keyed on `service`, never on text — nearly every subtask's description
    mentions tests, so a text rule would fire on ordinary implementation work."""
    project = _project(tmp_path, with_test_file=False)
    ordinary = {
        "id": "S1",
        "description": "Add the VAT quote endpoint and unit tests for rounding",
        "service": "backend",
        "files_to_create": ["docs/plans/030-design.md"],
    }
    assert testing_deliverable_gap(ordinary, project) is None


def test_a_testing_subtask_declaring_no_files_is_inert(tmp_path: Path):
    """Nothing declared is nothing to be dishonest about."""
    project = _project(tmp_path, with_test_file=False)
    assert (
        testing_deliverable_gap(
            {"id": "TEST", "description": "tests", "service": "testing"}, project
        )
        is None
    )


def test_the_escape_hatch_disables_it(tmp_path: Path, monkeypatch):
    """One switch for all the honesty gates, not one per gate."""
    monkeypatch.setenv("AIFACTORY_TEST_EVIDENCE_GATE", "off")
    project = _project(tmp_path, with_test_file=False)
    assert completion_refusal(PROSE_SUBTASK, project, RAN_GREEN) is None


def test_the_operator_reads_the_most_specific_refusal(tmp_path: Path):
    """Gate ordering. A testing subtask's text makes it a verification subtask to
    #851, so with #851 first the answer to "you shipped a document" would be "run
    pytest now" — true, and no help at all."""
    project = _project(tmp_path, with_test_file=False)
    never_ran = {"ran": False, "last_failed": False, "runs": 0, "last_command": None}
    refusal = completion_refusal(PROSE_SUBTASK, project, never_ran)
    assert refusal is not None
    assert "every file it declares is documentation" in refusal
    assert "no test command ran this build" not in refusal


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
