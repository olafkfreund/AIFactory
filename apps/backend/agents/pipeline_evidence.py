"""CI-pipeline evidence — the CI/CD subtask cannot be prose (#1113).

The Documenting Coder, live: on two independent builds of
``olafkfreund/aifactory-demo`` (specs ``101-vat-quote-endpoint`` and
``108-invoice-line-total-endpoint``) the ``CICD`` subtask committed a 343-line
and a 376-line design document under ``docs/plans/`` and never touched
``.github/workflows/ci.yml``. Its own acceptance criteria are about stages that
"run on every push and PR", so they were unsatisfiable by what it produced. QA
rejected sign-off both times and a QA-fix cycle then did the real work — the
same job paid for twice, on every feature.

It was not a model wobble. The subtask arrived from PFactory with
``files_to_create: ["docs/plans/<plan-id>-cicd-pipeline.md"]`` — the ONLY file
target in the whole record — because the child issue's body said "implement the
pipeline specified in <that document>" and PFactory's delta pass mines file
tokens straight out of the child's text. The coder was handed one file to
create, a markdown file, and created it. Any model would.

PFactory now names the real pipeline file instead. This module is the half that
does not depend on the planner getting it right: ``update_subtask_status``
refuses to complete a CI/CD subtask while the repo's pipeline is still missing
the stages that subtask promised. Evidence must match the claim (#851's shape),
and here the evidence is the pipeline file itself.

Narrow on purpose, because a gate that fires on correct work gets switched off:

* Only CI/CD subtasks are judged — ``service == "cicd"`` (the contract field) or
  text that names CI/CD outright. Every other subtask completes as before.
* Only stages the subtask's OWN text demands are required. A CI/CD subtask that
  asks for nothing specific is inert.
* A repo whose pipeline already covers the demanded stages passes immediately,
  with no edit at all — which is the honest answer when a feature's CI/CD
  subtask was appended to a repo that already has working CI.
* ``deploy`` is deliberately not checked: "gated on a green build and requires
  manual approval" is not decidable from a keyword, and a false block costs more
  than the miss.

Shares the ``AIFACTORY_TEST_EVIDENCE_GATE=off`` escape hatch with #851/#1111 —
one switch for "the honesty gates misfire on this repo", not a switch per gate.

Checked by ``tests/test_pipeline_evidence_gate.py``, whose fixtures are the two
live subtask records and the live ``ci.yml`` they failed to change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Pipeline files, in the order a repo is likely to use them. The first pattern
# is also the fallback path suggested when a repo has no pipeline at all.
_CI_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
    "bitbucket-pipelines.yml",
    "Jenkinsfile",
)
_DEFAULT_CI_PATH = ".github/workflows/ci.yml"

# A subtask this gate governs. ``service`` is the contract field PFactory sets;
# the text patterns catch a plan that describes CI/CD work without tagging it.
_CICD_SERVICES = frozenset({"cicd", "ci/cd", "ci-cd"})
_CICD_TEXT = re.compile(r"\bci/cd\b|\bci pipeline\b|\bgithub actions\b", re.IGNORECASE)

# stage name -> what counts as that stage being wired into a pipeline file.
# The values are tool names as they appear in a workflow, so a pipeline that
# runs `ruff check .` satisfies "lint" without containing the word "lint".
_STAGE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "lint": (
        "lint",
        "ruff",
        "eslint",
        "flake8",
        "pylint",
        "golangci",
        "clippy",
        "gofmt",
        "black",
        "prettier",
        "rubocop",
        "checkstyle",
    ),
    "test": (
        "test",
        "pytest",
        "jest",
        "vitest",
        "mocha",
        "rspec",
        "phpunit",
        "gotestsum",
        "nextest",
        "tox",
    ),
    "build": (
        "build",
        "docker build",
        "compile",
        "package",
        "bundle",
        "wheel",
        "buildx",
        "sdist",
    ),
    "security scan": (
        "security",
        "trivy",
        "bandit",
        "pip-audit",
        "npm audit",
        "codeql",
        "semgrep",
        "snyk",
        "gitleaks",
        "grype",
        "osv-scanner",
        "dependency-audit",
        "dependency-review",
        "sast",
        "secret-scan",
        "safety",
        "govulncheck",
        "cargo audit",
    ),
}

# How a subtask asks for a stage. Separate from the evidence patterns above: the
# subtask says "security-scan stages run on every push", the pipeline says
# "trivy". Matching one vocabulary against the other is the whole point.
_STAGE_DEMAND: dict[str, tuple[str, ...]] = {
    "lint": ("lint", "static analysis", "formatting check"),
    "test": ("test", "coverage"),
    "build": ("build", "compile", "package", "container image"),
    "security scan": (
        "security scan",
        "security-scan",
        "securityscan",
        "vulnerability",
        "dependency audit",
        "dependency-audit",
        "sast",
        "secret scan",
        "secret-scan",
        "scanned",
    ),
}

_MAX_CI_BYTES = 200_000


def gate_enabled() -> bool:
    """ON by default; shares the #851 escape hatch (see the module docstring)."""
    from agents.test_evidence import gate_enabled as _shared  # noqa: PLC0415

    return bool(_shared())


def is_cicd_subtask(subtask: dict[str, Any]) -> bool:
    """True when this subtask is about the CI/CD pipeline — the gated kind."""
    if str(subtask.get("service") or "").strip().lower() in _CICD_SERVICES:
        return True
    return bool(_CICD_TEXT.search(_subtask_text(subtask)))


def _subtask_text(subtask: dict[str, Any]) -> str:
    """Title + description + acceptance criteria, lowercased."""
    parts = [str(subtask.get(k) or "") for k in ("id", "title", "name", "description")]
    criteria = subtask.get("acceptance_criteria")
    if isinstance(criteria, list):
        parts += [str(c) for c in criteria]
    return "\n".join(parts).lower()


def demanded_stages(subtask: dict[str, Any]) -> list[str]:
    """Pipeline stages this subtask's own text promises will run in CI."""
    text = _subtask_text(subtask)
    return [
        stage
        for stage, phrases in _STAGE_DEMAND.items()
        if any(p in text for p in phrases)
    ]


def ci_files(project_dir: Path | str) -> list[Path]:
    """Pipeline files present in the repo, deterministically ordered."""
    root = Path(project_dir)
    found: list[Path] = []
    for pattern in _CI_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path.is_file() and path not in found:
                found.append(path)
    return found


def _pipeline_text(paths: list[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            if path.stat().st_size > _MAX_CI_BYTES:
                continue
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks).lower()


def missing_stages(subtask: dict[str, Any], project_dir: Path | str) -> list[str]:
    """Stages the subtask demands that no pipeline file provides evidence of."""
    text = _pipeline_text(ci_files(project_dir))
    return [
        stage
        for stage in demanded_stages(subtask)
        if not any(token in text for token in _STAGE_EVIDENCE[stage])
    ]


def pipeline_evidence_gap(
    subtask: dict[str, Any], project_dir: Path | str
) -> str | None:
    """Refusal reason when a CI/CD subtask's pipeline does not do what it claims.

    Returns ``None`` (complete freely) unless the subtask is a CI/CD subtask AND
    its own text demands stages the repo's pipeline files show no sign of. See
    the module docstring for why each condition keeps the gate off correct work.
    """
    if not is_cicd_subtask(subtask):
        return None
    wanted = demanded_stages(subtask)
    if not wanted:
        return None  # promises nothing specific — nothing to be dishonest about

    present = ci_files(project_dir)
    if not present:
        return (
            "Refused: this is the CI/CD subtask and it promises "
            f"{', '.join(wanted)} stages, but this repo has no pipeline file at "
            f"all. Create `{_DEFAULT_CI_PATH}` (or the pipeline file for this "
            "repo's CI system) and wire those stages into it. A design document "
            "under docs/ satisfies none of the acceptance criteria, which are "
            "about stages that RUN on push and PR (#1113). If the pipeline "
            "genuinely cannot be written, mark this subtask 'failed' with the "
            "reason rather than reporting unverified work complete (RFC-0006)."
        )

    gaps = missing_stages(subtask, project_dir)
    if not gaps:
        return None

    names = ", ".join(str(p) for p in present[:3])
    return (
        f"Refused: this is the CI/CD subtask and it promises {', '.join(wanted)} "
        f"stages, but the pipeline ({names}) has no {', '.join(gaps)} stage. Edit "
        "the pipeline file itself so those stages run on push and PR — that is "
        "what the acceptance criteria ask for, and a document describing the "
        "pipeline satisfies none of them (#1113: this exact subtask shipped a "
        "343-line design doc and an unchanged ci.yml, twice). Then re-run the "
        "linters/tests and complete this subtask, or mark it 'failed' with the "
        "reason."
    )
