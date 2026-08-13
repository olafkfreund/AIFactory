"""
Output-side DLP (#323, #310)
============================

AIFactory scans on the input/commit side (``git_validators.validate_git_commit``
runs the secret scanner over staged files). But a coding agent emits more
outbound text than commit *contents*: commit *messages*, PR title/body, and
PR/issue comments all leave the boundary unscanned and can exfiltrate a
credential or PII even when the committed files are clean. See
``docs/docs/compliance/output-dlp.md``.

This is the reuse seam the design proposes: pass outbound agent-authored text to
the existing ``scan_secrets.scan_content`` (every ``ALL_PATTERNS`` entry plus the
same false-positive filter used at commit time) — no new detector.

Default-safe, mirroring the commit scanner and model registry:
``AIFACTORY_OUTPUT_DLP`` resolves to ``warn`` (default: log a masked audit line,
do not block), ``block`` (refuse), or ``off`` (skip). A detected secret is never
silently passed — ``warn`` always logs it; ``block`` additionally refuses.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .scan_secrets import SecretMatch, redacted_fingerprint, scan_content

logger = logging.getLogger(__name__)

DLP_ENV = "AIFACTORY_OUTPUT_DLP"


def dlp_mode(env: Mapping[str, str] | None = None) -> str:
    """Resolve enforcement mode from ``AIFACTORY_OUTPUT_DLP``.

    Default is ``warn``; an unrecognised value also resolves to ``warn`` so a
    typo cannot silently disable scanning.
    """
    value = (env if env is not None else os.environ).get(DLP_ENV, "warn")
    value = value.strip().lower()
    return value if value in ("warn", "block", "off") else "warn"


@dataclass
class DLPResult:
    """Outcome of one outbound scan."""

    mode: str
    label: str
    matched: list[SecretMatch] = field(default_factory=list)

    @property
    def has_hit(self) -> bool:
        return bool(self.matched)

    @property
    def blocked(self) -> bool:
        """True when a hit must stop the text from leaving the boundary."""
        return self.has_hit and self.mode == "block"

    def summary(self) -> str:
        """Redacted, safe-to-log/surface one-line description of the matches.

        This line is emitted at WARNING by the DLP filter itself, so it lands in
        every log sink the deployment ships to. It carries the pattern name and
        ``file:line`` for triage and a non-reversible fingerprint of the match --
        never a prefix of it, which is what the previous ``mask_secret(..., 12)``
        wrote here.
        """
        return "; ".join(
            f"{m.pattern_name}@{m.file_path}:{m.line_number} "
            f"({redacted_fingerprint(m.matched_text)})"
            for m in self.matched
        )


def scan_outbound(
    text: str, label: str, env: Mapping[str, str] | None = None
) -> DLPResult:
    """Scan agent-authored outbound ``text`` for secrets/PII before it is sent.

    ``label`` names the vector (e.g. ``"commit-message:001-add-x"``,
    ``"pr-body:42"``) and is what ``scan_content`` reports as the file. On a hit
    the match is always logged (masked); the caller decides whether ``blocked``
    means refuse. Never raises — a scanner crash logs and passes (the commit-time
    scanner is likewise fail-open so DLP cannot break a build); ``warn`` is the
    default so nothing is blocked by accident.
    """
    mode = dlp_mode(env)
    if mode == "off" or not text:
        return DLPResult(mode=mode, label=label)
    try:
        matched = scan_content(text, label)
    except Exception:
        # ponytail: fail-open + log, matching git_validators' "don't break the
        # build" stance. Escalate to fail-closed only if block-mode misses matter.
        logger.exception("Output DLP scan crashed on %s", label)
        matched = []
    result = DLPResult(mode=mode, label=label, matched=matched)
    if result.has_hit:
        logger.warning(
            "Output DLP %s on %s: %s",
            "BLOCK" if result.blocked else "WARN",
            label,
            result.summary(),
        )
    return result
