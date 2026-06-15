"""RFC-0001a evidence gate for the build completion event.

A build may only claim a SUCCESS status if it carries proof it ran. A 0-token
"completed" is a dead build (e.g. an expired provider credential producing a stub
plan) and must be downgraded to failed so no consumer renders it green.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # web-server deps; installed in CI's backend gate

from server.services.completion import build_completion_event  # noqa: E402


def _event(status: str, total_tokens: int | None):
    usage = None if total_tokens is None else {"total_tokens": total_tokens, "cost_usd": 0.0}
    return build_completion_event(
        task_id="proj:spec-001", spec_id="spec-001", status=status,
        issue_number=42, usage=usage,
    )


def test_zero_token_completed_is_downgraded_to_failed():
    ev = _event("completed", 0)
    assert ev["status"] == "failed"
    assert "no_evidence" in (ev.get("halt_reason") or "")


def test_real_build_completed_passes_and_carries_evidence():
    ev = _event("completed", 1_513_998)
    assert ev["status"] == "completed"
    assert ev["evidence"]["proof_kind"] == "tokens"
    assert ev["evidence"]["total_tokens"] == 1_513_998


def test_failed_status_is_untouched_by_the_gate():
    # A genuine failure stays failed regardless of tokens.
    ev = _event("failed", 0)
    assert ev["status"] == "failed"


def test_no_usage_event_is_not_gated():
    # Non-build events (no usage) are out of scope — status is preserved.
    ev = _event("completed", None)
    assert ev["status"] == "completed"
    assert "evidence" not in ev
