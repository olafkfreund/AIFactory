"""
Pre-coder untrusted-content scan gate (#805 / Factory#273)
==========================================================

The coder clones untrusted brownfield repos and receives issue-derived spec
text. Indirect prompt injection via that content is OWASP's #1 LLM threat.
This module is the deterministic pre-coder scan: a short, testable
regex/heuristic pass over (a) the spec text (``spec.md``, where issue-derived
text lands) and (b) the high-risk repo prose the coder will read (README*,
CONTRIBUTING*, ``*.md`` at the repo root). Scanning code comments and the raw
task contract is v2.

Deliberately NO LLM call: a judge scanning for injection is itself injectable.

Fail-closed (RFC-0012 pattern): a scanner crash yields a ``flagged`` verdict,
never ``pass``. The gate is controlled by the ``AIFACTORY_INJECTION_SCAN``
env flag: ``on`` (default, flagged blocks to human_review), ``warn`` (stamp
the verdict but do not block), ``off`` (skip scanning, verdict ``skipped``).

The verdict is written to ``<spec_dir>/injection_scan.json`` so the
completion-event emitter can stamp ``injection_scan`` into the RFC-0001
envelope for CFactory.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCAN_ENV = "AIFACTORY_INJECTION_SCAN"
RESULT_FILENAME = "injection_scan.json"

# Deterministic injection-payload pattern list. Keep it short and testable;
# every entry needs a fixture in test_content_scan.py.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous_instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?"
            r"(?:previous|prior|above|earlier)\s+"
            r"(?:instructions?|prompts?|rules?|directives?)",
            re.IGNORECASE,
        ),
    ),
    (
        "role_reassignment",
        re.compile(r"\byou\s+are\s+(?:now|no\s+longer)\b", re.IGNORECASE),
    ),
    (
        "new_instructions_header",
        re.compile(
            r"\b(?:new|real|actual|updated)\s+(?:system\s+)?instructions\s*:",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_probe",
        re.compile(
            r"\b(?:reveal|print|show|repeat|output)\b[^.\n]{0,40}\bsystem\s+prompt\b",
            re.IGNORECASE,
        ),
    ),
    (
        "run_command_imperative",
        re.compile(r"\brun\s+the\s+following\s+command\b", re.IGNORECASE),
    ),
    (
        "curl_pipe_shell",
        re.compile(
            r"\b(?:curl|wget)\b[^\n|]{0,200}\|\s*(?:ba|z|da)?sh\b", re.IGNORECASE
        ),
    ),
    # Base64 blobs this long have no business in prose files (only prose is
    # scanned in v1, so lockfiles/signatures cannot false-positive here).
    ("base64_blob", re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")),
    (
        "credential_exfil_url",
        re.compile(
            r"https?://\S*[?&](?:token|secret|api_?key|password|credential)s?=",
            re.IGNORECASE,
        ),
    ),
    (
        "hide_from_human",
        re.compile(
            r"\bdo\s+not\s+(?:tell|show|inform|mention)\b[^.\n]{0,40}"
            r"\b(?:user|human|operator)\b",
            re.IGNORECASE,
        ),
    ),
    ("exfiltrate", re.compile(r"\bexfiltrat\w*\b", re.IGNORECASE)),
]

# High-risk repo prose the coder will read, at the repo root only (v1).
HIGH_RISK_GLOBS = ("README*", "CONTRIBUTING*", "*.md")

# Cap per-file read size; injection payloads live in prose, not 10 MB dumps.
_MAX_FILE_BYTES = 512 * 1024


@dataclass
class ScanResult:
    """Outcome of one pre-coder scan run."""

    verdict: str  # "pass" | "flagged" | "skipped"
    mode: str  # "on" | "warn" | "off"
    matched: list[dict[str, str]] = field(default_factory=list)

    @property
    def blocks(self) -> bool:
        """True when this verdict must stop the task before coding."""
        return self.verdict == "flagged" and self.mode == "on"

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "mode": self.mode, "matched": self.matched}


def scan_mode(env: Mapping[str, str] | None = None) -> str:
    """Resolve the gate mode from ``AIFACTORY_INJECTION_SCAN``.

    Default is ``on``; an unrecognised value also resolves to ``on``
    (fail-closed - a typo must never silently disable the gate).
    """
    value = (env if env is not None else os.environ).get(SCAN_ENV, "on")
    value = value.strip().lower()
    return value if value in ("on", "warn", "off") else "on"


def scan_text(text: str) -> list[str]:
    """Return the names of every pattern that matches ``text``."""
    return [name for name, pattern in PATTERNS if pattern.search(text)]


def _scan_targets(spec_dir: Path, project_dir: Path) -> list[Path]:
    """The prose files to scan: the spec text plus repo-root high-risk files."""
    targets: list[Path] = []
    spec_md = spec_dir / "spec.md"
    if spec_md.is_file():
        targets.append(spec_md)
    seen: set[Path] = set(targets)
    for glob in HIGH_RISK_GLOBS:
        for path in sorted(project_dir.glob(glob)):
            if path.is_file() and path not in seen:
                seen.add(path)
                targets.append(path)
    return targets


def run_scan_gate(spec_dir: Path, project_dir: Path) -> ScanResult:
    """Scan the task's untrusted content and persist the verdict.

    Never raises: a scanner crash is a ``flagged`` verdict (fail-closed), and
    the verdict-file write is best-effort (the caller acts on the returned
    result, not the file).
    """
    mode = scan_mode()
    if mode == "off":
        result = ScanResult(verdict="skipped", mode=mode)
    else:
        try:
            matched: list[dict[str, str]] = []
            for path in _scan_targets(spec_dir, project_dir):
                text = path.read_bytes()[:_MAX_FILE_BYTES].decode(
                    "utf-8", errors="replace"
                )
                for name in scan_text(text):
                    matched.append({"pattern": name, "file": path.name})
            verdict = "flagged" if matched else "pass"
            result = ScanResult(verdict=verdict, mode=mode, matched=matched)
        except Exception:
            logger.exception("Injection scan crashed - failing closed (flagged)")
            result = ScanResult(
                verdict="flagged",
                mode=mode,
                matched=[{"pattern": "scanner_error", "file": ""}],
            )

    try:
        (spec_dir / RESULT_FILENAME).write_text(json.dumps(result.to_dict(), indent=2))
    except OSError:
        logger.warning("Could not write %s to %s", RESULT_FILENAME, spec_dir)
    return result
