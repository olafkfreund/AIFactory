"""LLM PII redactor (Epic #35 #38 PR-2b).

Applies built-in regex patterns + operator-supplied custom patterns to
prompt + response strings BEFORE they're written into the audit row.

Scope discipline (per design §4)
--------------------------------

Redaction is applied to the AUDIT ROW ONLY, never to the outbound LLM
request body. A high-sensitivity tenant whose prompt contains PII
still sends that PII to the LLM provider — this is intrinsic to LLM
use. Documented as a known v1.1 limitation; v1.2 ships a
``scrubBeforeSend`` mode that applies the same redactor pre-call.

Built-in patterns (per design §4)
---------------------------------

- US SSN: ``\\d{3}-\\d{2}-\\d{4}`` (hyphenated only; bare 9-digit
  numbers are too false-positive-prone — they collide with order
  numbers, code IDs, etc.).
- Email: ``[\\w.+-]+@[\\w-]+\\.[\\w.-]+``.
- US phone: ``(\\d{3})[ -]?\\d{3}-\\d{4}`` and ``\\d{3}-\\d{3}-\\d{4}``
  (parens or hyphen forms; bare 10-digit numbers excluded).
- CC pattern explicitly omitted (reviewer finding #4): the original
  pattern matched any 13-16 digit numeric string (IPv4 CIDRs, code
  identifiers) and corrupted legitimate prompt content without Luhn
  validation. Operators with PCI data add Luhn-checked patterns via
  the operator extension hook. v1.2 ships a Luhn-validating CC
  pattern as a built-in.

Failure-safe contract (per design §"Failure-safe contract")
-----------------------------------------------------------

A failed redaction pass logs WARNING + returns the ORIGINAL text. The
audit hook still writes the row; the value is "audit captured PII"
rather than "audit missing entirely". Same pattern as #40/#41/#42/#43.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# WHY hyphenated-only SSN: bare 9-digit numbers collide with order
# numbers / record IDs / phone numbers internationally. The
# false-positive cost on a 9-digit-anything pattern destroys audit
# usefulness.
_BUILTIN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED_EMAIL]"),
    # US phone — parens form first, then hyphen form. Each is its own
    # pattern so the replacement is consistently "[REDACTED_PHONE]".
    (re.compile(r"\(\d{3}\)[ -]?\d{3}-\d{4}"), "[REDACTED_PHONE]"),
    (re.compile(r"\b\d{3}-\d{3}-\d{4}\b"), "[REDACTED_PHONE]"),
]


# Exposed for tests: the built-in tuples in raw (pattern_str,
# replacement) form. Tests assert that CC is NOT in this list.
BUILTIN_PATTERNS: list[tuple[str, str]] = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]"),
    (r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "[REDACTED_EMAIL]"),
    (r"\(\d{3}\)[ -]?\d{3}-\d{4}", "[REDACTED_PHONE]"),
    (r"\b\d{3}-\d{3}-\d{4}\b", "[REDACTED_PHONE]"),
]


class PiiRedactor:
    """Apply built-in + operator-supplied regex patterns to text.

    Construct once per process (or per request when patterns are
    request-scoped). All ``redact*`` methods are pure: they neither
    raise nor mutate state. On a regex failure, the input is
    returned unchanged (logged at WARNING).

    Parameters
    ----------
    extra_patterns:
        Optional list of ``(regex_string, replacement_string)`` tuples
        compiled at construct time. Operators wire these in via the
        Helm chart's ``litellm.audit.extraRedactionPatterns``. Invalid
        regexes are skipped with a WARNING — the constructor never
        raises, so a typo in operator config can't crash the audit
        writer.
    """

    def __init__(
        self,
        extra_patterns: list[tuple[str, str]] | None = None,
    ) -> None:
        # Start with the built-ins (already pre-compiled).
        self._patterns: list[tuple[re.Pattern[str], str]] = list(_BUILTIN_PATTERNS)

        for entry in extra_patterns or []:
            try:
                pattern_str, replacement = entry
            except (TypeError, ValueError):
                logger.warning(
                    "PiiRedactor: skipping malformed extra pattern entry %r "
                    "(expected (regex_string, replacement_string))",
                    entry,
                )
                continue
            try:
                compiled = re.compile(pattern_str)
            except re.error as exc:
                logger.warning(
                    "PiiRedactor: skipping invalid extra regex %r: %s",
                    pattern_str,
                    exc,
                )
                continue
            self._patterns.append((compiled, replacement))

    def redact(self, text: str) -> str:
        """Apply every pattern. Returns the redacted string.

        On any regex execution failure (re.error, unexpected
        exception), logs WARNING + returns the input unchanged. This
        matches the failure-safe contract: an audit row with PII is
        worse than no audit row, but an audit miss is worse still.
        """
        if not text:
            return text
        out = text
        for compiled, replacement in self._patterns:
            try:
                out = compiled.sub(replacement, out)
            except Exception:
                # WHY catch broad: an operator-supplied pattern could
                # trigger anything from re.error to a runaway pathological
                # backtrack timeout. Logging + bail keeps audit working.
                logger.warning(
                    "PiiRedactor: pattern %r failed during sub; skipping this pattern",
                    compiled.pattern,
                    exc_info=True,
                )
                continue
        return out

    def redact_dict(self, data: Any) -> Any:
        """Deep-redact a JSON-serializable value (dict / list / str / scalar).

        Useful for ``details_json`` payloads where free-form keys may
        carry PII inside nested structures. Non-string scalars are
        passed through unchanged.
        """
        if isinstance(data, str):
            return self.redact(data)
        if isinstance(data, dict):
            return {k: self.redact_dict(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.redact_dict(item) for item in data]
        # int / float / bool / None — nothing to redact.
        return data


__all__ = ["BUILTIN_PATTERNS", "PiiRedactor"]
