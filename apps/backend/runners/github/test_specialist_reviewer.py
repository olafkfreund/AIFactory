#!/usr/bin/env python3
"""
Tests for the shared specialist-reviewer framework.

These validate the behaviour-preserving extraction shared by the parallel
orchestrator and parallel follow-up PR reviewers:

- severity-string mapping
- prompt loading from prompts/github
- stable finding-id generation (orchestrator + follow-up formats)
- finding de-duplication
- the data-driven specialist agent registry

The reviewers run both as a flat module and as a package; this test imports the
shared module directly (flat) which mirrors how the bare-module test suite in
this directory is run.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

# Bootstrap sys.path so the shared module and its `models` dependency resolve
# regardless of the directory pytest is invoked from.
_GITHUB_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _GITHUB_DIR.parent.parent
for _p in (str(_BACKEND_DIR), str(_GITHUB_DIR / "services"), str(_GITHUB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import specialist_reviewer as sr  # noqa: E402
from models import ReviewSeverity  # noqa: E402


@dataclass
class _Finding:
    """Minimal duck-typed stand-in for PRReviewFinding (file/line/title)."""

    file: str
    line: int
    title: str


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------


def test_map_severity_known_values():
    assert sr.map_severity("critical") == ReviewSeverity.CRITICAL
    assert sr.map_severity("high") == ReviewSeverity.HIGH
    assert sr.map_severity("medium") == ReviewSeverity.MEDIUM
    assert sr.map_severity("low") == ReviewSeverity.LOW


def test_map_severity_is_case_insensitive():
    assert sr.map_severity("HIGH") == ReviewSeverity.HIGH
    assert sr.map_severity("Critical") == ReviewSeverity.CRITICAL


def test_map_severity_unknown_defaults_to_medium():
    assert sr.map_severity("bogus") == ReviewSeverity.MEDIUM


def test_map_severity_custom_default():
    # The follow-up comment path historically defaulted some entries to LOW.
    assert sr.map_severity("bogus", default=ReviewSeverity.LOW) == ReviewSeverity.LOW


# ---------------------------------------------------------------------------
# Finding-id generation (must match the two pre-extraction formats)
# ---------------------------------------------------------------------------


def test_generate_finding_id_orchestrator_format():
    """No prefix -> lower-case 12-char md5 digest (orchestrator format)."""
    digest = hashlib.md5(b"a.py:3:Title", usedforsecurity=False).hexdigest()
    assert sr.generate_finding_id("a.py", 3, "Title") == digest[:12]


def test_generate_finding_id_followup_format():
    """FU- prefix -> upper-case 8-char digest (follow-up format)."""
    digest = hashlib.md5(b"a.py:3:Title", usedforsecurity=False).hexdigest()
    assert (
        sr.generate_finding_id("a.py", 3, "Title", prefix="FU-")
        == f"FU-{digest[:8].upper()}"
    )


def test_generate_finding_id_is_deterministic():
    a = sr.generate_finding_id("x.py", 1, "T")
    b = sr.generate_finding_id("x.py", 1, "T")
    assert a == b


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------


def test_deduplicate_findings_drops_same_key():
    findings = [
        _Finding("a.py", 1, "Bug"),
        _Finding("a.py", 1, "bug"),  # title compared case-insensitively
        _Finding("a.py", 1, " Bug "),  # title is stripped
        _Finding("b.py", 1, "Bug"),  # different file -> kept
    ]
    unique = sr.deduplicate_findings(findings)
    assert len(unique) == 2
    assert (unique[0].file, unique[0].title) == ("a.py", "Bug")
    assert unique[1].file == "b.py"


def test_deduplicate_findings_preserves_order():
    findings = [_Finding("c.py", 9, "Z"), _Finding("a.py", 1, "A")]
    unique = sr.deduplicate_findings(findings)
    assert [f.file for f in unique] == ["c.py", "a.py"]


def test_deduplicate_findings_empty():
    assert sr.deduplicate_findings([]) == []


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------


def test_load_github_prompt_missing_returns_empty():
    assert sr.load_github_prompt("definitely-not-a-real-prompt-file.md") == ""


def test_load_github_prompt_reads_existing(tmp_path, monkeypatch):
    sample = tmp_path / "sample.md"
    sample.write_text("hello prompt", encoding="utf-8")
    monkeypatch.setattr(sr, "_PROMPTS_GITHUB_DIR", tmp_path)
    assert sr.load_github_prompt("sample.md") == "hello prompt"


# ---------------------------------------------------------------------------
# Data-driven specialist registry
# ---------------------------------------------------------------------------


def test_orchestrator_roster_names():
    agents = sr.build_specialist_agents(sr.ORCHESTRATOR_SPECIALISTS)
    assert set(agents) == {
        "security-reviewer",
        "quality-reviewer",
        "logic-reviewer",
        "codebase-fit-reviewer",
        "ai-triage-reviewer",
    }


def test_followup_roster_names():
    agents = sr.build_specialist_agents(sr.FOLLOWUP_SPECIALISTS)
    assert set(agents) == {
        "resolution-verifier",
        "new-code-reviewer",
        "comment-analyzer",
        "finding-validator",
    }


def test_built_agents_are_read_only_and_inherit_model():
    agents = sr.build_specialist_agents(sr.ORCHESTRATOR_SPECIALISTS)
    for agent in agents.values():
        assert agent.tools == ["Read", "Grep", "Glob"]
        assert agent.model == "inherit"
        assert agent.description  # non-empty


def test_build_agents_uses_fallback_when_prompt_missing():
    # The default loader returns "" for unknown files, so the fallback prompt
    # must be used verbatim.
    spec = sr.SpecialistAgentSpec(
        name="x",
        description="d",
        prompt_file="no-such-file.md",
        fallback_prompt="FALLBACK",
    )
    agents = sr.build_specialist_agents([spec])
    assert agents["x"].prompt == "FALLBACK"


def test_build_agents_prefers_loaded_prompt():
    spec = sr.SpecialistAgentSpec(
        name="x",
        description="d",
        prompt_file="any.md",
        fallback_prompt="FALLBACK",
    )
    agents = sr.build_specialist_agents([spec], prompt_loader=lambda _f: "LOADED")
    assert agents["x"].prompt == "LOADED"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
