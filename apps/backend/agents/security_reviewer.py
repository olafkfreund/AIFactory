"""
Security Reviewer — pre-merge diff gate (Phase 5 of the self-healing pipeline;
tracking #397)
============================================================================

A Codex/Copilot-style "reviewer agent before merge": scan the final diff for
secrets, injection, unsafe deserialization, and other risky patterns, and gate
the merge on high-severity findings. Composes with the dynamic command allowlist
(runtime defense) and the Phase 4 ``pre_merge_gate`` (path-based) — this adds
content-based defense in depth.

Two layers:
- ``scan_diff_static`` — fast, deterministic regex pre-scan over the *added*
  lines of a unified diff. No LLM, no I/O — always available, fully testable, and
  the floor of the gate.
- ``review_diff`` — optionally augments the static findings with an LLM
  subagent (best-effort, injected client factory) for semantic issues (authz
  gaps, logic flaws). Failures degrade gracefully to the static result.

``gate_decision`` turns findings into a block/allow verdict at a severity
threshold; the result feeds the risk-tier pre-merge gate and build_report (#397).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Severity ordering (low index = less severe).
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_BLOCK_THRESHOLD = "high"


@dataclass
class SecurityFinding:
    severity: str
    category: str
    detail: str
    file: str | None = None
    line: int | None = None
    source: str = "static"  # "static" | "llm"


@dataclass
class GateDecision:
    blocked: bool
    threshold: str
    blocking: list[SecurityFinding] = field(default_factory=list)
    summary: str = ""


@dataclass
class SecurityReport:
    findings: list[SecurityFinding] = field(default_factory=list)
    decision: GateDecision | None = None

    def as_report(self) -> list[dict[str, Any]]:
        """Findings for build_report.json (#397)."""
        return [asdict(f) for f in self.findings]


# (severity, category, compiled pattern). Conservative — aimed at obvious,
# newly-introduced issues to keep false positives low.
_STATIC_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "critical",
        "secret:private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    ("critical", "secret:aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "high",
        "secret:hardcoded",
        re.compile(
            r"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|token)"
            r"\s*[:=]\s*['\"][^'\"]{6,}['\"]"
        ),
    ),
    ("high", "injection:eval", re.compile(r"\beval\s*\(")),
    ("high", "injection:exec", re.compile(r"(?<![A-Za-z_.])exec\s*\(")),
    ("high", "injection:os-system", re.compile(r"\bos\.system\s*\(")),
    ("high", "injection:shell-true", re.compile(r"shell\s*=\s*True")),
    ("medium", "deserialize:pickle", re.compile(r"\bpickle\.loads?\s*\(")),
    ("medium", "deserialize:yaml-unsafe", re.compile(r"yaml\.load\s*\((?![^)]*Safe)")),
    ("medium", "tls:verify-disabled", re.compile(r"verify\s*=\s*False")),
    ("low", "config:debug-on", re.compile(r"(?i)\bdebug\s*=\s*True\b")),
]

# Diff hunk header: @@ -a,b +c,d @@ — gives the new-file starting line.
_HUNK_RE = re.compile(r"^@@ .*\+(\d+)(?:,\d+)? @@")


def _iter_added_lines(diff_text: str):
    """Yield (file, new_line_no, added_text) for each added line in a unified diff."""
    current_file: str | None = None
    new_lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current_file = path[2:] if path.startswith("b/") else path
            continue
        if raw.startswith("--- "):
            continue
        m = _HUNK_RE.match(raw)
        if m:
            new_lineno = int(m.group(1))
            continue
        if raw.startswith("+"):
            yield current_file, new_lineno, raw[1:]
            new_lineno += 1
        elif raw.startswith("-"):
            continue  # removed line — doesn't advance new-file counter
        else:
            new_lineno += 1  # context line


def scan_diff_static(diff_text: str) -> list[SecurityFinding]:
    """Deterministic regex scan over the added lines of a unified diff."""
    findings: list[SecurityFinding] = []
    for file, lineno, text in _iter_added_lines(diff_text):
        for severity, category, pattern in _STATIC_RULES:
            if pattern.search(text):
                findings.append(
                    SecurityFinding(
                        severity=severity,
                        category=category,
                        detail=text.strip()[:200],
                        file=file,
                        line=lineno,
                        source="static",
                    )
                )
    return findings


def severity_at_least(severity: str, threshold: str) -> bool:
    return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(threshold, 99)


def gate_decision(
    findings: list[SecurityFinding], *, threshold: str = DEFAULT_BLOCK_THRESHOLD
) -> GateDecision:
    """Block the merge if any finding meets/exceeds ``threshold`` severity."""
    blocking = [f for f in findings if severity_at_least(f.severity, threshold)]
    if blocking:
        cats = ", ".join(sorted({f.category for f in blocking}))
        summary = f"{len(blocking)} finding(s) >= {threshold}: {cats}"
    else:
        summary = f"no findings >= {threshold} ({len(findings)} total)"
    return GateDecision(
        blocked=bool(blocking), threshold=threshold, blocking=blocking, summary=summary
    )


async def review_diff(
    diff_text: str,
    *,
    project_dir: Path | None = None,
    threshold: str = DEFAULT_BLOCK_THRESHOLD,
    llm_scan: Callable[[str], Any] | None = None,
) -> SecurityReport:
    """Full review: static scan, optionally augmented by an LLM subagent.

    ``llm_scan`` is an injected ``async (diff) -> list[SecurityFinding]`` (default
    None = static only). The integration layer passes a function that runs a
    reviewer subagent via ``core.client.create_client``; any failure there is
    swallowed so the static gate still stands.
    """
    findings = scan_diff_static(diff_text)
    if llm_scan is not None:
        try:
            extra = await llm_scan(diff_text)
            if extra:
                findings.extend(extra)
        except Exception as exc:  # noqa: BLE001 - LLM augmentation is best-effort
            logger.warning("[security] LLM scan failed, using static only: %s", exc)
    decision = gate_decision(findings, threshold=threshold)
    return SecurityReport(findings=findings, decision=decision)
