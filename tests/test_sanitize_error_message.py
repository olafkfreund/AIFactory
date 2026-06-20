#!/usr/bin/env python3
"""Tests for agents.base.sanitize_error_message after the factory_common adoption.

``sanitize_error_message`` now redacts in two layers (epic Factory#154, issue
Factory#161):

1. the canonical fleet redaction (vendored ``factory_common.secrets.redact`` - a
   *superset* pattern table), then
2. the pre-existing AIFactory-specific rules (``sk-``/``key-`` API keys, bare
   ``Bearer``/``token=``/``secret=`` values), preserved verbatim.

These tests pin BOTH halves: every prior behaviour still holds (the AIFactory
placeholders are unchanged for the shapes they owned), AND the credential shapes
that previously leaked through this function are now redacted by the fleet layer.
"""

from __future__ import annotations

from agents.base import sanitize_error_message

# The fleet placeholder (factory_common.secrets.PLACEHOLDER), inlined so the test
# does not couple to the vendored package's import path.
FLEET_REDACTED = "***REDACTED***"


# --- behaviour preserved: AIFactory-specific rules keep their exact output -----


def test_empty_string_returns_empty() -> None:
    assert sanitize_error_message("") == ""


def test_sk_api_key_redacted_with_original_placeholder() -> None:
    out = sanitize_error_message("boom sk-ant-api03-AAAABBBBCCCCDDDDEEEE done")
    assert "sk-ant-api03" not in out
    assert "[REDACTED_API_KEY]" in out


def test_key_prefixed_api_key_redacted() -> None:
    out = sanitize_error_message("key-abcdefghijklmnopqrstuvwxyz12")
    assert out == "[REDACTED_API_KEY]"


def test_bare_bearer_token_keeps_aifactory_placeholder() -> None:
    # A bare "Bearer <tok>" (no "Authorization:" prefix) is NOT in the fleet
    # table, so the AIFactory rule must still own it.
    out = sanitize_error_message("Bearer abcdefghijklmnopqrstuvwxyz123")
    assert out == "Bearer [REDACTED_TOKEN]"


def test_token_assignment_redacted() -> None:
    out = sanitize_error_message("token=abcdefghijklmnopqrstuvwxyz12")
    assert out == "token=[REDACTED_TOKEN]"


def test_secret_assignment_redacted() -> None:
    out = sanitize_error_message("secret: abcdefghijklmnopqrstuvwxyz12")
    assert out == "secret: [REDACTED_SECRET]"


def test_truncates_to_max_length_plus_ellipsis() -> None:
    out = sanitize_error_message("x" * 600, max_length=500)
    assert len(out) == 503
    assert out.endswith("...")


def test_plain_message_unchanged() -> None:
    assert sanitize_error_message("a normal error with no secrets") == (
        "a normal error with no secrets"
    )


# --- NEW superset coverage: shapes that previously leaked are now redacted -----


def test_github_pat_now_redacted() -> None:
    leaked = "git push failed: ghp_" + "A" * 36
    out = sanitize_error_message(leaked)
    assert "ghp_" not in out
    assert FLEET_REDACTED in out


def test_aws_access_key_now_redacted() -> None:
    out = sanitize_error_message("creds AKIA" + "A" * 16)
    assert "AKIA" not in out
    assert FLEET_REDACTED in out


def test_url_userinfo_credential_now_redacted() -> None:
    out = sanitize_error_message("clone https://user:supersecretpw@host/repo.git")
    assert "supersecretpw" not in out
    # The scheme + user context survives; only the password span is replaced.
    assert "https://user:" in out
    assert FLEET_REDACTED in out


def test_authorization_bearer_header_now_redacted() -> None:
    # "Authorization: Bearer <tok>" IS in the fleet table, which redacts the
    # token span; the secret must be gone regardless of which placeholder wins.
    out = sanitize_error_message("Authorization: Bearer abcdefghijklmnop12345")
    assert "abcdefghijklmnop12345" not in out
