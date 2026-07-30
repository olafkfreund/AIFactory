"""LLM PII redactor (Epic #35 #38 PR-2b; v1.2 #210 Luhn + scrubBeforeSend).

Applies built-in regex patterns + operator-supplied custom patterns to
prompt + response strings BEFORE they're written into the audit row.
Optionally (v1.2 ``scrub_outbound=True``) the same redactor runs on
the prompt BEFORE it's sent to the LLM provider — see the
``scrubBeforeSend`` mode section below.

Scope discipline (per design §4)
--------------------------------

v1.1 (default behaviour): redaction is applied to the AUDIT ROW ONLY,
never to the outbound LLM request body. A high-sensitivity tenant
whose prompt contains PII still sends that PII to the LLM provider —
this is intrinsic to LLM use. Documented as a known v1.1 limitation.

v1.2 (opt-in via ``scrub_outbound=True``): the same redactor runs on
the outbound prompt BEFORE the API call. This closes the
"LLM still sees plaintext" caveat for orgs that opt in, at the cost
of some prompt-quality degradation on heavy redaction. The audit row
continues to capture both raw + redacted so operators can verify what
was actually sent versus what was logged.

Built-in patterns (per design §4 + v1.2 #210)
---------------------------------------------

- US SSN: ``\\d{3}-\\d{2}-\\d{4}`` (hyphenated only; bare 9-digit
  numbers are too false-positive-prone — they collide with order
  numbers, code IDs, etc.).
- Email: ``[\\w.+-]+@[\\w-]+\\.[\\w.-]+``.
- US phone: ``(\\d{3})[ -]?\\d{3}-\\d{4}`` and ``\\d{3}-\\d{3}-\\d{4}``
  (parens or hyphen forms; bare 10-digit numbers excluded).
- Credit card (v1.2): 13-19 digit sequences with optional spaces /
  dashes, validated via Luhn checksum. A naked digit run that fails
  Luhn is left UNCHANGED — this closes the v1.1 false-positive issue
  (IPv4 CIDRs, code identifiers, random hashes) that caused the
  pattern to be dropped from the v1.1 built-ins. Luhn check is heavy
  compared to a simple regex, so the CC pass runs LAST in the chain
  (cheap patterns short-circuit first).

Failure-safe contract (per design §"Failure-safe contract")
-----------------------------------------------------------

A failed redaction pass logs WARNING + returns the ORIGINAL text. The
audit hook still writes the row; the value is "audit captured PII"
rather than "audit missing entirely". Same pattern as #40/#41/#42/#43.

That trade is inverted on the OUTBOUND path (#320). ``redact_outbound``
runs with ``strict=True``: a pass that cannot run re-raises rather than
returning the original, because "return the original" there means the
raw prompt goes to a third-party LLM with only a log line to show for
it. Inbound/audit redaction keeps the fail-open contract above.
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
# replacement) form. The Luhn-CC pass is NOT a simple regex sub — it's
# a callable applied separately by ``_redact_credit_cards``. See
# ``BUILTIN_CC_REPLACEMENT`` for the constant test code asserts on.
BUILTIN_PATTERNS: list[tuple[str, str]] = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]"),
    (r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "[REDACTED_EMAIL]"),
    (r"\(\d{3}\)[ -]?\d{3}-\d{4}", "[REDACTED_PHONE]"),
    (r"\b\d{3}-\d{3}-\d{4}\b", "[REDACTED_PHONE]"),
]

# v1.2 CC pattern label. Exposed so tests + operator docs reference
# the same string.
BUILTIN_CC_REPLACEMENT: str = "[REDACTED_CC]"


# Matches a digit-run of 13-19 numerals with optional spaces or dashes
# between digits. The {12,18} count covers digit-separator-digit pairs
# after the leading digit, totalling 13-19 digits before Luhn check.
# Word boundary anchors prevent a 20-digit run from being partially
# matched as a 16-digit CC.
_CC_RAW_PATTERN: re.Pattern[str] = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_check(digits: str) -> bool:
    """Standard Luhn checksum. ``digits`` must be a bare digit string.

    Doubles every second digit from the right, subtracts 9 from any
    result > 9, and sums. Valid iff the total is divisible by 10.
    Returns False for any non-digit input rather than raising — the
    caller has already stripped separators.
    """
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact_credit_cards(text: str) -> str:
    """Replace Luhn-valid 13-19 digit sequences with ``[REDACTED_CC]``.

    A digit run that matches the raw pattern but fails Luhn is left
    UNCHANGED. This is the v1.2 fix for the v1.1 false-positive issue:
    IPv4 CIDRs (``192.168.1.0/24``), code identifiers, random hashes,
    and arbitrary digit blobs no longer trigger redaction.
    """

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"[ -]", "", raw)
        # Belt-and-braces length re-check: the raw pattern can match a
        # 13-19 digit span, but a future regex tweak shouldn't slip a
        # 12- or 20-digit string into the Luhn path.
        if 13 <= len(digits) <= 19 and _luhn_check(digits):
            return BUILTIN_CC_REPLACEMENT
        return raw

    return _CC_RAW_PATTERN.sub(_replace, text)


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
    scrub_outbound:
        v1.2 #210 opt-in flag. When True, ``redact_outbound()`` is a
        functional alias of ``redact()`` and the call-site
        (``OpenAICompatibleProvider`` etc.) applies the redactor to
        the prompt BEFORE the LLM API call. When False (default), the
        outbound prompt is sent verbatim and only the audit row is
        redacted — the v1.1 behaviour. Stored as ``self.scrub_outbound``
        so the call-site can branch on it without re-reading env vars.
    """

    def __init__(
        self,
        extra_patterns: list[tuple[str, str]] | None = None,
        scrub_outbound: bool = False,
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

        # v1.2 #210: opt-in pre-send scrub. Stored on the instance so
        # provider call-sites read the same flag they were configured
        # with rather than re-resolving env vars per call.
        self.scrub_outbound: bool = bool(scrub_outbound)

    def redact(self, text: str, *, strict: bool = False) -> str:
        """Apply every pattern. Returns the redacted string.

        On any regex execution failure (re.error, unexpected
        exception), logs WARNING + returns the input unchanged. This
        matches the failure-safe contract: an audit row with PII is
        worse than no audit row, but an audit miss is worse still.

        ``strict=True`` inverts that trade for the OUTBOUND call site
        (#320). Fail-open is correct when the text is on its way into
        our own audit table; it is a silent PII-egress bug when the
        text is on its way to a third-party LLM. Under ``strict`` a
        pattern failure re-raises so the caller can refuse to send.
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
                if strict:
                    raise
                continue
        # v1.2 #210: Luhn-checked CC pass runs LAST. The Luhn arithmetic
        # is more expensive than a single regex.sub, so the cheap
        # patterns above short-circuit first — text that's already lost
        # its non-CC PII is the only input that reaches the CC scan.
        try:
            out = _redact_credit_cards(out)
        except Exception:
            logger.warning(
                "PiiRedactor: CC/Luhn pass failed; leaving text unchanged",
                exc_info=True,
            )
            if strict:
                raise
        return out

    def redact_outbound(self, text: str) -> str:
        """Pre-send scrub for the outbound call site (#210, #320).

        Same patterns as ``redact()`` but ``strict=True``: a redaction
        pass that cannot run raises instead of returning the input
        unchanged. Returning the input here would put the raw prompt on
        the wire to the LLM provider with nothing but a WARNING in the
        log -- the exact silent fail-open #320 closes. The provider
        call-site turns the exception into a refusal to send.
        """
        return self.redact(text, strict=True)

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


__all__ = [
    "BUILTIN_CC_REPLACEMENT",
    "BUILTIN_PATTERNS",
    "PiiRedactor",
]
