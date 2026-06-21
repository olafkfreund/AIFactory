"""
Specialist Reviewer Framework
=============================

Shared building blocks for the SDK-subagent PR reviewers (parallel
orchestrator + parallel follow-up). The two reviewers had independently
copy-pasted the same specialist-framework helpers; this module is the single
source of truth for them:

- ``SEVERITY_MAPPING`` / ``map_severity`` — AI severity-string normalisation.
- ``load_github_prompt`` — read an agent prompt from ``prompts/github``.
- ``report_progress`` — emit a ``ProgressCallback`` to the orchestrator.
- ``deduplicate_findings`` — drop findings with the same (file, line, title).
- ``generate_finding_id`` — stable md5-based finding identifier.
- ``SpecialistAgentSpec`` + ``build_specialist_agents`` — a data-driven
  registry that turns prompt-backed specs into SDK ``AgentDefinition`` objects.
- ``ORCHESTRATOR_SPECIALISTS`` / ``FOLLOWUP_SPECIALISTS`` — the two concrete
  agent rosters previously hard-coded inside each reviewer.

The extraction is behaviour-preserving: the call sites delegate to these
helpers and keep their public entrypoints (``review``) unchanged.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import AgentDefinition

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from runners.github.models import PRReviewFinding

# ``ReviewSeverity`` is needed at runtime (it keys ``SEVERITY_MAPPING``). The
# reviewers run both as a flat module (``services`` on sys.path) and as a
# package (``runners.github.services``); import flat first and fall back to the
# package path via importlib so the parent-relative import stays absolute (no
# parent-relative ``from ..`` import, which the strict baseline forbids).
try:
    from models import ReviewSeverity
except ImportError:
    ReviewSeverity = importlib.import_module("runners.github.models").ReviewSeverity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity mapping
# ---------------------------------------------------------------------------

# Maps AI-emitted severity strings to the ReviewSeverity enum. Unknown values
# fall back to MEDIUM (the historical default used by both reviewers).
SEVERITY_MAPPING: dict[str, ReviewSeverity] = {
    "critical": ReviewSeverity.CRITICAL,
    "high": ReviewSeverity.HIGH,
    "medium": ReviewSeverity.MEDIUM,
    "low": ReviewSeverity.LOW,
}


def map_severity(
    severity_str: str, default: ReviewSeverity = ReviewSeverity.MEDIUM
) -> ReviewSeverity:
    """Map a (possibly mixed-case) severity string to ``ReviewSeverity``."""
    return SEVERITY_MAPPING.get(severity_str.lower(), default)


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

# prompts/github lives four levels up from this module
# (services -> github -> runners -> backend).
_PROMPTS_GITHUB_DIR = Path(__file__).parent.parent.parent.parent / "prompts" / "github"


def load_github_prompt(filename: str) -> str:
    """Load a prompt file from the ``prompts/github`` directory.

    Returns an empty string (and logs a warning) when the file is missing,
    matching the historical fallback behaviour of both reviewers.
    """
    prompt_file = _PROMPTS_GITHUB_DIR / filename
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8")
    logger.warning("Prompt file not found: %s", prompt_file)
    return ""


# ---------------------------------------------------------------------------
# Finding helpers
# ---------------------------------------------------------------------------


def generate_finding_id(file: str, line: int, title: str, prefix: str = "") -> str:
    """Build a stable finding id from its location and title.

    ``prefix`` lets callers namespace ids (the follow-up reviewer uses ``"FU-"``
    with an upper-cased 8-char digest; the orchestrator uses no prefix with a
    lower-case 12-char digest). The default reproduces the orchestrator format.
    """
    content = f"{file}:{line}:{title}"
    digest = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
    if prefix:
        return f"{prefix}{digest[:8].upper()}"
    return digest[:12]


def deduplicate_findings(
    findings: Iterable[PRReviewFinding],
) -> list[PRReviewFinding]:
    """Drop findings sharing the same (file, line, normalised title)."""
    seen: set[tuple[str, int, str]] = set()
    unique: list[PRReviewFinding] = []
    for finding in findings:
        key = (finding.file, finding.line, finding.title.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


# ---------------------------------------------------------------------------
# Data-driven specialist agent registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecialistAgentSpec:
    """Declarative description of one SDK specialist subagent.

    ``prompt_file`` is loaded from ``prompts/github``; ``fallback_prompt`` is
    used verbatim when that file is missing. All review specialists are
    read-only and inherit the orchestrator's model, so those defaults are baked
    in but remain overridable.
    """

    name: str
    description: str
    prompt_file: str
    fallback_prompt: str
    tools: tuple[str, ...] = ("Read", "Grep", "Glob")
    model: str = "inherit"


def build_specialist_agents(
    specs: Sequence[SpecialistAgentSpec],
    prompt_loader: Callable[[str], str] = load_github_prompt,
) -> dict[str, AgentDefinition]:
    """Turn ``specs`` into the SDK ``agents={}`` mapping.

    Each spec's prompt is loaded via ``prompt_loader`` (defaults to
    ``load_github_prompt``), falling back to ``fallback_prompt`` when the file
    is absent — exactly as the hand-written ``_define_specialist_agents``
    methods did.
    """
    agents: dict[str, AgentDefinition] = {}
    for spec in specs:
        agents[spec.name] = AgentDefinition(
            description=spec.description,
            prompt=prompt_loader(spec.prompt_file) or spec.fallback_prompt,
            tools=list(spec.tools),
            model=spec.model,
        )
    return agents


# Roster for the full-PR parallel orchestrator reviewer.
ORCHESTRATOR_SPECIALISTS: tuple[SpecialistAgentSpec, ...] = (
    SpecialistAgentSpec(
        name="security-reviewer",
        description=(
            "Security specialist. Use for OWASP Top 10, authentication, "
            "injection, cryptographic issues, and sensitive data exposure. "
            "Invoke when PR touches auth, API endpoints, user input, database queries, "
            "or file operations."
        ),
        prompt_file="pr_security_agent.md",
        fallback_prompt="You are a security expert. Find vulnerabilities.",
    ),
    SpecialistAgentSpec(
        name="quality-reviewer",
        description=(
            "Code quality expert. Use for complexity, duplication, error handling, "
            "maintainability, and pattern adherence. Invoke when PR has complex logic, "
            "large functions, or significant business logic changes."
        ),
        prompt_file="pr_quality_agent.md",
        fallback_prompt="You are a code quality expert. Find quality issues.",
    ),
    SpecialistAgentSpec(
        name="logic-reviewer",
        description=(
            "Logic and correctness specialist. Use for algorithm verification, "
            "edge cases, state management, and race conditions. Invoke when PR has "
            "algorithmic changes, data transformations, concurrent operations, or bug fixes."
        ),
        prompt_file="pr_logic_agent.md",
        fallback_prompt="You are a logic expert. Find correctness issues.",
    ),
    SpecialistAgentSpec(
        name="codebase-fit-reviewer",
        description=(
            "Codebase consistency expert. Use for naming conventions, ecosystem fit, "
            "architectural alignment, and avoiding reinvention. Invoke when PR introduces "
            "new patterns, large additions, or code that might duplicate existing functionality."
        ),
        prompt_file="pr_codebase_fit_agent.md",
        fallback_prompt="You are a codebase expert. Check for consistency.",
    ),
    SpecialistAgentSpec(
        name="ai-triage-reviewer",
        description=(
            "AI comment validator. Use for triaging comments from CodeRabbit, "
            "Gemini Code Assist, Cursor, Greptile, and other AI reviewers. "
            "Invoke when PR has existing AI review comments that need validation."
        ),
        prompt_file="pr_ai_triage.md",
        fallback_prompt="You are an AI triage expert. Validate AI comments.",
    ),
)


# Roster for the incremental parallel follow-up reviewer.
FOLLOWUP_SPECIALISTS: tuple[SpecialistAgentSpec, ...] = (
    SpecialistAgentSpec(
        name="resolution-verifier",
        description=(
            "Resolution verification specialist. Use to verify whether previous "
            "findings have been addressed. Analyzes diffs to determine if issues "
            "are truly fixed, partially fixed, or still unresolved. "
            "Invoke when: There are previous findings to verify."
        ),
        prompt_file="pr_followup_resolution_agent.md",
        fallback_prompt="You verify whether previous findings are resolved.",
    ),
    SpecialistAgentSpec(
        name="new-code-reviewer",
        description=(
            "New code analysis specialist. Reviews code added since last review "
            "for security, logic, quality issues, and regressions. "
            "Invoke when: There are substantial code changes (>50 lines diff) or "
            "changes to security-sensitive areas."
        ),
        prompt_file="pr_followup_newcode_agent.md",
        fallback_prompt="You review new code for issues.",
    ),
    SpecialistAgentSpec(
        name="comment-analyzer",
        description=(
            "Comment and feedback analyst. Processes contributor comments and "
            "AI tool reviews (CodeRabbit, Cursor, Gemini, etc.) to identify "
            "unanswered questions and valid concerns. "
            "Invoke when: There are comments or formal reviews since last review."
        ),
        prompt_file="pr_followup_comment_agent.md",
        fallback_prompt="You analyze comments and feedback.",
    ),
    SpecialistAgentSpec(
        name="finding-validator",
        description=(
            "Finding re-investigation specialist. Re-investigates unresolved findings "
            "to validate they are actually real issues, not false positives. "
            "Actively reads the code at the finding location with fresh eyes. "
            "Can confirm findings as valid OR dismiss them as false positives. "
            "CRITICAL: Invoke for ALL unresolved findings after resolution-verifier runs. "
            "Invoke when: There are findings marked as unresolved that need validation."
        ),
        prompt_file="pr_finding_validator.md",
        fallback_prompt="You validate whether unresolved findings are real issues.",
    ),
)
