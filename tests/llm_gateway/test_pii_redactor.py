"""Tests for the LLM PII redactor (Epic #35 #38 PR-2b; v1.2 #210).

Covers:
- Built-in SSN / email / phone pattern coverage.
- Operator-supplied extra patterns are merged + applied.
- Deep-redact on nested dict / list structures.
- A regex failure on an operator pattern does NOT raise; passthrough.
- v1.2 #210: Luhn-validated credit-card pass redacts valid CC numbers
  (hyphen / space / no-separator forms) and leaves Luhn-invalid digit
  runs unchanged. The v1.1 negative test
  ``test_cc_pattern_intentionally_omitted_from_builtins`` flipped to
  ``test_luhn_invalid_left_unchanged`` to reflect the new contract.
- v1.2 #210: ``redact_outbound`` alias + ``scrub_outbound`` flag.
- Construction with a malformed (regex_str, replacement) tuple skips
  the bad entry + logs WARNING; the constructor never raises.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.llm_pii_redactor import (
    BUILTIN_CC_REPLACEMENT,
    BUILTIN_PATTERNS,
    PiiRedactor,
)

# ---------------------------------------------------------------------------
# Built-in patterns
# ---------------------------------------------------------------------------


def test_ssn_hyphenated_is_redacted():
    r = PiiRedactor()
    out = r.redact("Patient SSN: 123-45-6789 on file.")
    assert "123-45-6789" not in out
    assert "[REDACTED_SSN]" in out


def test_bare_9_digit_number_is_NOT_redacted_as_ssn():
    """Design §4: bare 9-digit numbers are too false-positive-prone.
    Only the hyphenated form is in the built-ins."""
    r = PiiRedactor()
    out = r.redact("Order number: 123456789")
    assert "123456789" in out
    assert "[REDACTED_SSN]" not in out


def test_email_is_redacted():
    r = PiiRedactor()
    out = r.redact("Contact alice@example.com for details.")
    assert "alice@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_phone_parens_form_is_redacted():
    r = PiiRedactor()
    out = r.redact("Call (555) 123-4567 tomorrow.")
    assert "(555) 123-4567" not in out
    assert "[REDACTED_PHONE]" in out


def test_phone_hyphen_form_is_redacted():
    r = PiiRedactor()
    out = r.redact("Backup: 555-123-4567 if no answer.")
    assert "555-123-4567" not in out
    assert "[REDACTED_PHONE]" in out


def test_multiple_patterns_in_same_text():
    r = PiiRedactor()
    out = r.redact(
        "User alice@example.com (SSN 123-45-6789) at (555) 123-4567",
    )
    assert "[REDACTED_EMAIL]" in out
    assert "[REDACTED_SSN]" in out
    assert "[REDACTED_PHONE]" in out
    assert "alice@example.com" not in out


def test_empty_string_passes_through():
    assert PiiRedactor().redact("") == ""


def test_text_without_pii_passes_through():
    r = PiiRedactor()
    sample = "The quick brown fox jumps over the lazy dog."
    assert r.redact(sample) == sample


# ---------------------------------------------------------------------------
# Luhn-validated credit-card pattern — v1.2 #210
# ---------------------------------------------------------------------------


def test_luhn_credit_card_redacted():
    """A Luhn-valid Visa test number (hyphen separators) is redacted."""
    r = PiiRedactor()
    out = r.redact("Card on file: 4111-1111-1111-1111 expires soon.")
    assert "4111-1111-1111-1111" not in out
    assert BUILTIN_CC_REPLACEMENT in out


def test_luhn_credit_card_with_spaces_redacted():
    """Space-separated CC form is normalised + redacted on Luhn pass."""
    r = PiiRedactor()
    out = r.redact("Card on file: 4111 1111 1111 1111.")
    assert "4111 1111 1111 1111" not in out
    assert BUILTIN_CC_REPLACEMENT in out


def test_luhn_credit_card_no_separator_redacted():
    """Bare 16-digit Luhn-valid number is redacted (no separator form)."""
    r = PiiRedactor()
    out = r.redact("Raw PAN: 4111111111111111 end.")
    assert "4111111111111111" not in out
    assert BUILTIN_CC_REPLACEMENT in out


def test_luhn_invalid_left_unchanged():
    """v1.1 → v1.2 flip: this was previously
    ``test_cc_pattern_intentionally_omitted_from_builtins``. The v1.2
    Luhn-validated CC pass redacts valid numbers but a digit run that
    fails Luhn is left UNCHANGED — closing the v1.1 false-positive
    problem (IPv4 CIDRs, hashes, code IDs were corrupted by the
    pre-Luhn naive pattern). ``4111-1111-1111-1112`` differs from
    the canonical Visa test number by the last digit and fails Luhn."""
    r = PiiRedactor()
    out = r.redact("Not a card: 4111-1111-1111-1112 leave this alone.")
    assert "4111-1111-1111-1112" in out
    assert BUILTIN_CC_REPLACEMENT not in out


def test_short_numeric_not_redacted_even_if_luhn():
    """The CC regex requires 13-19 digits. A 4-digit number must not
    be touched even if it happened to pass Luhn — the false-positive
    rate on sub-13-digit runs is far too high."""
    r = PiiRedactor()
    # 4 digits, won't be matched by the CC regex regardless of Luhn.
    out = r.redact("Code: 1230 next step.")
    assert "1230" in out
    assert BUILTIN_CC_REPLACEMENT not in out


def test_long_numeric_not_redacted():
    """A 24-digit run is outside the 13-19 CC window. Must not be
    matched even if the leading 16 digits happen to be Luhn-valid."""
    r = PiiRedactor()
    out = r.redact("Hash: 123456789012345678901234 done.")
    assert "123456789012345678901234" in out
    assert BUILTIN_CC_REPLACEMENT not in out


def test_negative_pattern_v11_compat():
    """v1.1 false-positive case: a random 14-digit number (often
    encountered as an internal id, request token, or partial hash)
    that is Luhn-invalid stays unchanged. v1.1's behaviour
    (CC pattern dropped) and v1.2's behaviour (Luhn-checked CC
    pattern) converge on this input — the v1.1 contract holds."""
    r = PiiRedactor()
    out = r.redact("Request id: 12345678901234.")
    assert "12345678901234" in out
    assert BUILTIN_CC_REPLACEMENT not in out


def test_cc_pattern_not_in_simple_builtin_list():
    """``BUILTIN_PATTERNS`` lists only the simple regex.sub patterns.
    The CC pass is a separate function (``_redact_credit_cards``) so
    Luhn validation is in the loop. Tests that introspect
    ``BUILTIN_PATTERNS`` should not see the CC replacement label."""
    for _, replacement in BUILTIN_PATTERNS:
        assert replacement != BUILTIN_CC_REPLACEMENT


# ---------------------------------------------------------------------------
# Operator-supplied extra patterns
# ---------------------------------------------------------------------------


def test_extra_patterns_are_applied():
    r = PiiRedactor(extra_patterns=[(r"ACC-\d{8}", "[REDACTED_ACCT]")])
    out = r.redact("Internal account: ACC-12345678 on the books.")
    assert "ACC-12345678" not in out
    assert "[REDACTED_ACCT]" in out


def test_extras_compose_with_builtins():
    """Built-ins still apply when extras are supplied."""
    r = PiiRedactor(extra_patterns=[(r"ACC-\d{8}", "[REDACTED_ACCT]")])
    out = r.redact("alice@example.com + ACC-12345678")
    assert "[REDACTED_EMAIL]" in out
    assert "[REDACTED_ACCT]" in out


def test_malformed_extra_pattern_entry_is_skipped():
    """A 1-tuple or non-tuple entry must NOT raise at construction."""
    # Should log WARNING but proceed.
    r = PiiRedactor(extra_patterns=[("only-one-element",)])  # type: ignore[list-item]
    # Built-ins still work.
    out = r.redact("Email: bob@example.com")
    assert "[REDACTED_EMAIL]" in out


def test_invalid_regex_extra_pattern_is_skipped():
    """An invalid regex string must NOT raise at construction."""
    r = PiiRedactor(
        extra_patterns=[
            ("(unbalanced", "[X]"),
            (r"ACC-\d{8}", "[REDACTED_ACCT]"),
        ],
    )
    # The valid extra still applies; the invalid one was skipped.
    out = r.redact("ACC-12345678")
    assert "[REDACTED_ACCT]" in out


# ---------------------------------------------------------------------------
# Deep-redact dict
# ---------------------------------------------------------------------------


def test_redact_dict_handles_nested_structures():
    r = PiiRedactor()
    payload = {
        "user": {
            "email": "alice@example.com",
            "phone": "(555) 123-4567",
        },
        "notes": [
            "SSN: 123-45-6789",
            "no pii here",
        ],
        "count": 42,
        "active": True,
        "tags": None,
    }
    out = r.redact_dict(payload)
    assert out["user"]["email"] == "[REDACTED_EMAIL]"
    assert out["user"]["phone"] == "[REDACTED_PHONE]"
    assert out["notes"][0] == "SSN: [REDACTED_SSN]"
    assert out["notes"][1] == "no pii here"
    # Non-string scalars pass through unchanged.
    assert out["count"] == 42
    assert out["active"] is True
    assert out["tags"] is None


def test_redact_dict_handles_string_input_at_top_level():
    """redact_dict on a plain string just redacts."""
    r = PiiRedactor()
    out = r.redact_dict("alice@example.com")
    assert out == "[REDACTED_EMAIL]"


# ---------------------------------------------------------------------------
# Failure-safe contract
# ---------------------------------------------------------------------------


def test_redact_does_not_raise_on_internal_pattern_error(monkeypatch):
    """If a pattern's .sub() raises mid-redaction, the redactor logs
    WARNING + returns whatever has been redacted so far."""
    r = PiiRedactor()

    # Monkeypatch one of the compiled patterns to raise on sub().
    import re

    class _BoomPattern:
        pattern = "boom"

        def sub(self, _replacement, _text):
            raise re.error("synthetic failure")

    r._patterns = [(_BoomPattern(), "[X]"), *r._patterns]  # type: ignore[list-item]
    # Should not raise.
    out = r.redact("alice@example.com")
    # Subsequent built-ins still applied after the bad one was skipped.
    assert "[REDACTED_EMAIL]" in out


# ---------------------------------------------------------------------------
# scrubBeforeSend mode — v1.2 #210
# ---------------------------------------------------------------------------


def test_scrub_outbound_defaults_false():
    """v1.1-compat: scrubBeforeSend is opt-in. Default constructor
    leaves ``scrub_outbound`` False so existing callers see no change."""
    r = PiiRedactor()
    assert r.scrub_outbound is False


def test_scrub_outbound_flag_persists():
    """``scrub_outbound=True`` is exposed on the instance so the
    provider call-site can branch on it without re-reading env vars."""
    r = PiiRedactor(scrub_outbound=True)
    assert r.scrub_outbound is True


def test_redact_outbound_matches_redact():
    """``redact_outbound`` is a documentation alias of ``redact``.
    Same input → same output for both, including the v1.2 CC pass."""
    r = PiiRedactor(scrub_outbound=True)
    sample = "Card 4111-1111-1111-1111 owner alice@example.com."
    assert r.redact_outbound(sample) == r.redact(sample)
    out = r.redact_outbound(sample)
    assert BUILTIN_CC_REPLACEMENT in out
    assert "[REDACTED_EMAIL]" in out
