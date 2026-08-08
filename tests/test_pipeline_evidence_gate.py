"""#1113: the CI/CD subtask cannot be satisfied with a design document.

Two builds of ``olafkfreund/aifactory-demo`` (specs ``101-vat-quote-endpoint``
and ``108-invoice-line-total-endpoint``) completed their ``CICD`` subtask by
committing a markdown pipeline spec under ``docs/plans/`` while
``.github/workflows/ci.yml`` kept its single ``pytest -q`` job. The subtask's own
acceptance criteria are about stages that run on push and PR, so QA rejected
sign-off both times and a QA-fix cycle redid the work for real.

The subtask records and the ``ci.yml`` fixture below are the live artifacts,
copied verbatim from ``implementation_plan.json`` on the AIFactory workspace.
"""

import json
from pathlib import Path

import pytest
from agents.pipeline_evidence import (
    demanded_stages,
    is_cicd_subtask,
    missing_stages,
    pipeline_evidence_gap,
)
from agents.tools_pkg.tools.subtask import apply_subtask_status_update

# Verbatim from spec 101's implementation_plan.json — including the files_to_create
# that told the coder a markdown file was its only deliverable.
_CICD_SUBTASK = {
    "id": "CICD",
    "title": "",
    "status": "in_progress",
    "service": "cicd",
    "acceptance_criteria": [
        "Lint, test, build, and security-scan stages run on every push and PR.",
        "The test stage runs the full suite and publishes a coverage report.",
        "Deploy stages are gated on a green build and require manual approval.",
    ],
    "files_to_create": [
        "docs/plans/030-vat-quote-endpoint-with-half-up-money-rounding-cicd-pipeline.md"
    ],
}

_FEATURE_SUBTASK = {
    "id": "C2",
    "status": "in_progress",
    "description": "Add POST /api/quote VAT endpoint with half-up rounding",
    "acceptance_criteria": ["AC2: monetary values round half-up to 2 decimals."],
}

# aifactory-demo's .github/workflows/ci.yml, as the CICD subtask left it.
_CI_BEFORE = """name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest -q
"""

# ...and as the QA-fix cycle had to rewrite it.
_CI_AFTER = (
    _CI_BEFORE
    + """  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
  build:
    runs-on: ubuntu-latest
    steps:
      - run: docker build -t app .
  security-scan:
    runs-on: ubuntu-latest
    steps:
      - run: pip-audit
"""
)


def _repo(tmp_path: Path, ci: str | None = _CI_BEFORE) -> Path:
    if ci is not None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(ci)
    return tmp_path


def _spec(tmp_path: Path, subtask: dict) -> Path:
    spec = tmp_path / ".aifactory" / "specs" / "s1"
    spec.mkdir(parents=True)
    plan = {"phases": [{"id": "p1", "name": "CI/CD", "subtasks": [dict(subtask)]}]}
    (spec / "implementation_plan.json").write_text(json.dumps(plan))
    return spec


# ── what the gate judges ────────────────────────────────────────────────────


def test_cicd_subtask_is_recognised_by_its_contract_service_field():
    assert is_cicd_subtask(_CICD_SUBTASK)
    assert is_cicd_subtask({"description": "Set up CI/CD for the quote endpoint"})
    assert not is_cicd_subtask(_FEATURE_SUBTASK)


def test_demanded_stages_come_from_the_subtasks_own_criteria():
    assert demanded_stages(_CICD_SUBTASK) == ["lint", "test", "build", "security scan"]
    # A CI/CD subtask promising nothing specific demands nothing.
    assert demanded_stages({"service": "cicd", "description": "Set up CI/CD"}) == []


# ── the live defect ─────────────────────────────────────────────────────────


def test_design_document_does_not_satisfy_the_cicd_subtask(tmp_path):
    root = _repo(tmp_path)
    docs = root / "docs" / "plans"
    docs.mkdir(parents=True)
    (docs / "030-cicd-pipeline.md").write_text(
        "# CI/CD Pipeline\n\nStages: lint, test, build, security scan, deploy.\n"
    )

    gap = pipeline_evidence_gap(_CICD_SUBTASK, root)

    assert gap is not None
    assert missing_stages(_CICD_SUBTASK, root) == ["lint", "build", "security scan"]


def test_editing_the_pipeline_satisfies_it(tmp_path):
    root = _repo(tmp_path, ci=_CI_AFTER)
    assert pipeline_evidence_gap(_CICD_SUBTASK, root) is None


def test_repo_with_no_pipeline_is_told_which_file_to_write(tmp_path):
    gap = pipeline_evidence_gap(_CICD_SUBTASK, _repo(tmp_path, ci=None))
    assert gap and ".github/workflows/ci.yml" in gap


def test_pipeline_that_already_covers_the_stages_needs_no_edit(tmp_path):
    """A CI/CD subtask appended to a repo with complete CI completes as a no-op."""
    assert pipeline_evidence_gap(_CICD_SUBTASK, _repo(tmp_path, ci=_CI_AFTER)) is None


# ── staying off correct work ────────────────────────────────────────────────


def test_feature_subtask_is_never_judged(tmp_path):
    root = _repo(tmp_path)  # deliberately the lint/build/scan-less pipeline
    assert pipeline_evidence_gap(_FEATURE_SUBTASK, root) is None


def test_gate_is_inert_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_TEST_EVIDENCE_GATE", "off")
    root = _repo(tmp_path)
    spec = _spec(root, _CICD_SUBTASK)

    async def _run():
        return await apply_subtask_status_update(spec, "CICD", "completed", "", root)

    import asyncio

    assert "Successfully updated" in asyncio.run(_run())["content"][0]["text"]


# ── the gate on the real completion path ────────────────────────────────────


@pytest.mark.asyncio
async def test_update_subtask_status_refuses_the_documenting_coder(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, _CICD_SUBTASK)

    result = await apply_subtask_status_update(spec, "CICD", "completed", "", root)

    assert "Refused" in result["content"][0]["text"]
    plan = json.loads((spec / "implementation_plan.json").read_text())
    # A refusal must leave the plan untouched, or the refusal is cosmetic.
    assert plan["phases"][0]["subtasks"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_update_subtask_status_allows_a_real_pipeline_edit(tmp_path):
    root = _repo(tmp_path, ci=_CI_AFTER)
    spec = _spec(root, _CICD_SUBTASK)

    result = await apply_subtask_status_update(spec, "CICD", "completed", "", root)

    assert "Successfully updated" in result["content"][0]["text"]
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["phases"][0]["subtasks"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_is_never_blocked(tmp_path):
    """Honestly reporting failure must always be available (RFC-0006)."""
    root = _repo(tmp_path)
    spec = _spec(root, _CICD_SUBTASK)

    result = await apply_subtask_status_update(
        spec, "CICD", "failed", "no CI toolchain", root
    )

    assert "Successfully updated" in result["content"][0]["text"]
