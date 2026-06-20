"""Anti-loop / no-progress guardrail for the Act loop (#474, Hermes-inspired).

AIFactory's Act loop (coder → qa_reviewer → qa_fixer) and the TFactory handback
cycle can *spin*: an agent retries a failing edit, or re-reads the same file,
making no real progress until a hard cycle cap trips. Bounded retry
(``TFACTORY_HANDBACK_MAX_CYCLES``) and assertion-pinning (#467/#283) stop *test
drift* but not *no-progress spinning*.

This is the borrowed pattern from NousResearch/hermes-agent's
``agent/tool_guardrails.py``: a controller keyed on a tool-call **signature**
(tool name + args hash, plus result hash for idempotent reads) running three
named policies:

  - ``repeated_exact_failure`` — the *same* call fails identically ≥ N times → block.
  - ``same_tool_failure``      — *any* call of one tool fails ≥ M times this turn → halt.
  - ``idempotent_no_progress`` — a read-only call returns the *same* result ≥ K times → block.

Decisions are ``allow | warn | block | halt``. ``block`` refuses just that call
(fed back to the agent as a reason); ``halt`` ends the turn/loop early with a
typed reason that rides into the RFC-0001 completion event so CFactory can show
*why* a WorkItem stalled — instead of silently burning the whole cycle budget.

Pure, dependency-free, and SDK-agnostic: the PreToolUse hook calls
``before_call`` (it sees inputs → input-only policies), the PostToolUse hook
calls ``after_call`` (it sees outcomes → failure/result policies). Thresholds are
env-tunable (``AIFACTORY_GUARDRAIL_*``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Decision",
    "GuardrailVerdict",
    "ToolCallGuardrailController",
    "ToolCallSignature",
    "signature_for",
]

# Tools whose result is a pure function of their args — repeating one with an
# identical result is "no progress". Everything else is treated as mutating
# (Bash is ambiguous, so it is NOT idempotent: a build/test command legitimately
# repeats and its output legitimately changes).
_IDEMPOTENT_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})


class Decision(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"  # refuse this one call; the agent gets the reason
    HALT = "halt"  # end the turn/loop early with a typed reason


@dataclass(frozen=True)
class ToolCallSignature:
    tool: str
    args_hash: str

    def key(self) -> str:
        return f"{self.tool}:{self.args_hash}"


def _hash(obj) -> str:
    try:
        blob = json.dumps(obj, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(obj)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def signature_for(tool: str, tool_input) -> ToolCallSignature:
    """Stable signature for a tool call from its name + arguments."""
    return ToolCallSignature(tool=tool, args_hash=_hash(tool_input))


@dataclass(frozen=True)
class GuardrailVerdict:
    decision: Decision
    policy: str | None = None
    reason: str | None = None

    @property
    def stop(self) -> bool:
        return self.decision in (Decision.BLOCK, Decision.HALT)


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass
class _SigState:
    fail_streak: int = 0  # consecutive identical-args failures
    last_result_hash: str | None = None
    same_result_streak: int = 0  # idempotent: identical result in a row


@dataclass
class ToolCallGuardrailController:
    """Tracks tool-call signatures across a turn and rules on each call.

    One controller per Act-loop turn/session. ``before_call`` runs in the
    PreToolUse hook (inputs only); ``after_call`` runs in the PostToolUse hook
    (outcome known). ``halt_reason`` is set the moment a HALT fires so the loop
    can break and stamp the completion event.
    """

    repeated_exact_failure: int = field(
        default_factory=lambda: _env_int("AIFACTORY_GUARDRAIL_REPEAT_FAIL", 5)
    )
    same_tool_failure: int = field(
        default_factory=lambda: _env_int("AIFACTORY_GUARDRAIL_TOOL_FAIL_HALT", 8)
    )
    idempotent_no_progress: int = field(
        default_factory=lambda: _env_int("AIFACTORY_GUARDRAIL_NOPROGRESS", 5)
    )

    _sigs: dict[str, _SigState] = field(default_factory=dict)
    _tool_fail: dict[str, int] = field(default_factory=dict)
    halt_reason: str | None = None

    # ── PreToolUse: inputs only ──────────────────────────────────────────────
    def before_call(self, tool: str, tool_input) -> GuardrailVerdict:
        if self.halt_reason:
            return GuardrailVerdict(Decision.HALT, "halted", self.halt_reason)
        sig = signature_for(tool, tool_input)
        st = self._sigs.get(sig.key())

        # same-tool failure → halt the turn (the tool is fundamentally stuck).
        if self._tool_fail.get(tool, 0) >= self.same_tool_failure:
            return self._halt(
                "same_tool_failure",
                f"{tool} failed {self._tool_fail[tool]}× this turn — halting (no progress)",
            )

        # this exact call already failed N× → block just this repeat.
        if st and st.fail_streak >= self.repeated_exact_failure:
            return GuardrailVerdict(
                Decision.BLOCK,
                "repeated_exact_failure",
                f"This identical {tool} call has failed {st.fail_streak}× — try a different approach.",
            )

        # idempotent read that keeps returning the same thing → block (no progress).
        if (
            tool in _IDEMPOTENT_TOOLS
            and st
            and st.same_result_streak >= self.idempotent_no_progress
        ):
            return GuardrailVerdict(
                Decision.BLOCK,
                "idempotent_no_progress",
                f"{tool} returned the same result {st.same_result_streak}× — re-reading isn't making progress.",
            )
        return GuardrailVerdict(Decision.ALLOW)

    # ── PostToolUse: outcome known ───────────────────────────────────────────
    def after_call(
        self, tool: str, tool_input, *, ok: bool, result=None
    ) -> GuardrailVerdict:
        sig = signature_for(tool, tool_input)
        st = self._sigs.setdefault(sig.key(), _SigState())
        if ok:
            st.fail_streak = 0
            self._tool_fail[tool] = 0
            if tool in _IDEMPOTENT_TOOLS:
                rh = _hash(result)
                st.same_result_streak = (
                    st.same_result_streak + 1 if rh == st.last_result_hash else 1
                )
                st.last_result_hash = rh
                if st.same_result_streak >= self.idempotent_no_progress:
                    return GuardrailVerdict(
                        Decision.WARN,
                        "idempotent_no_progress",
                        f"{tool} has returned the same result {st.same_result_streak}× in a row.",
                    )
        else:
            st.fail_streak += 1
            self._tool_fail[tool] = self._tool_fail.get(tool, 0) + 1
            if self._tool_fail[tool] >= self.same_tool_failure:
                return self._halt(
                    "same_tool_failure",
                    f"{tool} failed {self._tool_fail[tool]}× this turn — halting (no progress)",
                )
            if st.fail_streak >= self.repeated_exact_failure:
                return GuardrailVerdict(
                    Decision.WARN,
                    "repeated_exact_failure",
                    f"This identical {tool} call has now failed {st.fail_streak}×.",
                )
        return GuardrailVerdict(Decision.ALLOW)

    def _halt(self, policy: str, reason: str) -> GuardrailVerdict:
        self.halt_reason = reason
        return GuardrailVerdict(Decision.HALT, policy, reason)
