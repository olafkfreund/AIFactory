#!/usr/bin/env python3
"""
Tests for the self-heal loop (Phase 3 of #397)
==============================================

Bounded-autonomy policy: checkpoint → attempt → verify → retry (bounded) →
rollback + escalate on exhaustion; security/destructive units escalate
immediately. Pure orchestrator tested with fakes (no git, no subprocesses).
"""

import pytest
from agents.self_heal import (
    ESCALATED,
    ESCALATED_IMMEDIATE,
    SUCCESS,
    run_with_self_heal,
)


class _Verify:
    """Fake verify result with a .passed flag and .failures list."""

    def __init__(self, passed, failures=None):
        self.passed = passed
        self.failures = failures or []


def _verifier(sequence):
    """Return an async verify() that yields the given results in order."""
    calls = {"n": 0}

    async def verify():
        i = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[i]

    return verify


async def _noop_attempt(n):
    return None


class TestSuccessPaths:
    async def test_passes_first_try(self):
        out = await run_with_self_heal(
            label="st1",
            attempt=_noop_attempt,
            verify=_verifier([_Verify(True)]),
        )
        assert out.status == SUCCESS
        assert out.attempts == 1
        assert out.rolled_back is False

    async def test_passes_on_retry(self):
        out = await run_with_self_heal(
            label="st1",
            attempt=_noop_attempt,
            verify=_verifier([_Verify(False, ["pytest"]), _Verify(True)]),
            max_attempts=3,
        )
        assert out.status == SUCCESS
        assert out.attempts == 2


class TestEscalation:
    async def test_exhausts_then_rolls_back_and_escalates(self):
        rolled = {"done": False}

        def snapshot(label):
            return {"ref": "abc", "label": label}

        def rollback(cp):
            rolled["done"] = True
            return True

        out = await run_with_self_heal(
            label="wave-1",
            attempt=_noop_attempt,
            verify=_verifier([_Verify(False, ["pytest"])]),
            snapshot=snapshot,
            rollback=rollback,
            max_attempts=2,
        )
        assert out.status == ESCALATED
        assert out.attempts == 2
        assert out.rolled_back is True
        assert rolled["done"] is True

    async def test_escalate_immediate_skips_attempts(self):
        attempts = {"n": 0}

        async def attempt(n):
            attempts["n"] += 1

        out = await run_with_self_heal(
            label="migration",
            attempt=attempt,
            verify=_verifier([_Verify(True)]),
            escalate_immediately=lambda: True,
        )
        assert out.status == ESCALATED_IMMEDIATE
        assert out.attempts == 0
        assert attempts["n"] == 0  # never attempted

    async def test_no_rollback_when_no_checkpoint(self):
        out = await run_with_self_heal(
            label="st1",
            attempt=_noop_attempt,
            verify=_verifier([_Verify(False)]),
            max_attempts=1,
        )
        assert out.status == ESCALATED
        assert out.rolled_back is False


class TestBehaviorDetails:
    async def test_attempt_exception_counts_as_failure(self):
        async def boom(n):
            raise RuntimeError("kaboom")

        out = await run_with_self_heal(
            label="st1",
            attempt=boom,
            verify=_verifier([_Verify(True)]),  # verify never reached on crash
            max_attempts=2,
        )
        assert out.status == ESCALATED
        assert any(e["event"] == "attempt_error" for e in out.events)

    async def test_diagnose_called_between_attempts_only(self):
        diag_calls = []

        async def diagnose(n):
            diag_calls.append(n)

        await run_with_self_heal(
            label="st1",
            attempt=_noop_attempt,
            verify=_verifier([_Verify(False), _Verify(False), _Verify(False)]),
            diagnose=diagnose,
            max_attempts=3,
        )
        # Diagnosed after attempts 1 and 2, not after the final attempt 3.
        assert diag_calls == [1, 2]

    async def test_events_log_for_report(self):
        out = await run_with_self_heal(
            label="wave-1",
            attempt=_noop_attempt,
            verify=_verifier([_Verify(False)]),
            snapshot=lambda label: {"ref": "x"},
            rollback=lambda cp: True,
            max_attempts=1,
        )
        kinds = [e["event"] for e in out.events]
        assert "checkpoint" in kinds
        assert "verify_failed" in kinds
        assert "rollback" in kinds
        assert "escalate" in kinds


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
