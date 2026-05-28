"""Tests for the LLM PII redactor (Epic #35 #38 PR-2b).

Covers:
- Built-in SSN / email / phone pattern coverage.
- Operator-supplied extra patterns are merged + applied.
- Deep-redact on nested dict / list structures.
- A regex failure on an operator pattern does NOT raise; passthrough.
- Explicit negative test: CC pattern is INTENTIONALLY omitted from
  the built-ins (design §4 reviewer finding #4) — a raw 13-16-digit
  numeric string must NOT be redacted.
- Construction with a malformed (regex_str, replacement) tuple skips
  the bad entry + logs WARNING; the constructor never raises.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / 'apps' / 'backend'
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.llm_pii_redactor import BUILTIN_PATTERNS, PiiRedactor

# ---------------------------------------------------------------------------
# Built-in patterns
# ---------------------------------------------------------------------------


def test_ssn_hyphenated_is_redacted():
    r = PiiRedactor()
    out = r.redact('Patient SSN: 123-45-6789 on file.')
    assert '123-45-6789' not in out
    assert '[REDACTED_SSN]' in out


def test_bare_9_digit_number_is_NOT_redacted_as_ssn():
    """Design §4: bare 9-digit numbers are too false-positive-prone.
    Only the hyphenated form is in the built-ins."""
    r = PiiRedactor()
    out = r.redact('Order number: 123456789')
    assert '123456789' in out
    assert '[REDACTED_SSN]' not in out


def test_email_is_redacted():
    r = PiiRedactor()
    out = r.redact('Contact alice@example.com for details.')
    assert 'alice@example.com' not in out
    assert '[REDACTED_EMAIL]' in out


def test_phone_parens_form_is_redacted():
    r = PiiRedactor()
    out = r.redact('Call (555) 123-4567 tomorrow.')
    assert '(555) 123-4567' not in out
    assert '[REDACTED_PHONE]' in out


def test_phone_hyphen_form_is_redacted():
    r = PiiRedactor()
    out = r.redact('Backup: 555-123-4567 if no answer.')
    assert '555-123-4567' not in out
    assert '[REDACTED_PHONE]' in out


def test_multiple_patterns_in_same_text():
    r = PiiRedactor()
    out = r.redact(
        'User alice@example.com (SSN 123-45-6789) at (555) 123-4567',
    )
    assert '[REDACTED_EMAIL]' in out
    assert '[REDACTED_SSN]' in out
    assert '[REDACTED_PHONE]' in out
    assert 'alice@example.com' not in out


def test_empty_string_passes_through():
    assert PiiRedactor().redact('') == ''


def test_text_without_pii_passes_through():
    r = PiiRedactor()
    sample = 'The quick brown fox jumps over the lazy dog.'
    assert r.redact(sample) == sample


# ---------------------------------------------------------------------------
# CC negative test — design §4 reviewer finding #4
# ---------------------------------------------------------------------------


def test_cc_pattern_intentionally_omitted_from_builtins():
    """CC pattern was dropped in v1.1 because the original
    `\\b(?:\\d[ -]*?){13,16}\\b` matched IPv4 CIDRs + code IDs +
    legitimate identifiers without Luhn validation. The built-ins
    list MUST NOT contain a CC pattern."""
    for pattern_str, _ in BUILTIN_PATTERNS:
        # Heuristic: the original buggy CC pattern had {13,16}.
        # The replacement label was [REDACTED_CC].
        assert '13' not in pattern_str or 'd{3}' in pattern_str
    # And the redactor itself doesn't touch a 16-digit string.
    r = PiiRedactor()
    out = r.redact('Test number: 4532015112830366')
    assert '4532015112830366' in out
    assert '[REDACTED_CC]' not in out


# ---------------------------------------------------------------------------
# Operator-supplied extra patterns
# ---------------------------------------------------------------------------


def test_extra_patterns_are_applied():
    r = PiiRedactor(extra_patterns=[(r'ACC-\d{8}', '[REDACTED_ACCT]')])
    out = r.redact('Internal account: ACC-12345678 on the books.')
    assert 'ACC-12345678' not in out
    assert '[REDACTED_ACCT]' in out


def test_extras_compose_with_builtins():
    """Built-ins still apply when extras are supplied."""
    r = PiiRedactor(extra_patterns=[(r'ACC-\d{8}', '[REDACTED_ACCT]')])
    out = r.redact('alice@example.com + ACC-12345678')
    assert '[REDACTED_EMAIL]' in out
    assert '[REDACTED_ACCT]' in out


def test_malformed_extra_pattern_entry_is_skipped():
    """A 1-tuple or non-tuple entry must NOT raise at construction."""
    # Should log WARNING but proceed.
    r = PiiRedactor(extra_patterns=[('only-one-element',)])  # type: ignore[list-item]
    # Built-ins still work.
    out = r.redact('Email: bob@example.com')
    assert '[REDACTED_EMAIL]' in out


def test_invalid_regex_extra_pattern_is_skipped():
    """An invalid regex string must NOT raise at construction."""
    r = PiiRedactor(
        extra_patterns=[
            ('(unbalanced', '[X]'),
            (r'ACC-\d{8}', '[REDACTED_ACCT]'),
        ],
    )
    # The valid extra still applies; the invalid one was skipped.
    out = r.redact('ACC-12345678')
    assert '[REDACTED_ACCT]' in out


# ---------------------------------------------------------------------------
# Deep-redact dict
# ---------------------------------------------------------------------------


def test_redact_dict_handles_nested_structures():
    r = PiiRedactor()
    payload = {
        'user': {
            'email': 'alice@example.com',
            'phone': '(555) 123-4567',
        },
        'notes': [
            'SSN: 123-45-6789',
            'no pii here',
        ],
        'count': 42,
        'active': True,
        'tags': None,
    }
    out = r.redact_dict(payload)
    assert out['user']['email'] == '[REDACTED_EMAIL]'
    assert out['user']['phone'] == '[REDACTED_PHONE]'
    assert out['notes'][0] == 'SSN: [REDACTED_SSN]'
    assert out['notes'][1] == 'no pii here'
    # Non-string scalars pass through unchanged.
    assert out['count'] == 42
    assert out['active'] is True
    assert out['tags'] is None


def test_redact_dict_handles_string_input_at_top_level():
    """redact_dict on a plain string just redacts."""
    r = PiiRedactor()
    out = r.redact_dict('alice@example.com')
    assert out == '[REDACTED_EMAIL]'


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
        pattern = 'boom'

        def sub(self, _replacement, _text):
            raise re.error('synthetic failure')

    r._patterns = [(_BoomPattern(), '[X]'), *r._patterns]  # type: ignore[list-item]
    # Should not raise.
    out = r.redact('alice@example.com')
    # Subsequent built-ins still applied after the bad one was skipped.
    assert '[REDACTED_EMAIL]' in out
