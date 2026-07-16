"""SDK hook bridge for the Act-loop hardening features (#474 / #475 / #476).

Wires three Hermes-inspired modules into the Claude Agent SDK's hook points
(PreToolUse / PostToolUse / PreCompact). Each feature is **flag-gated and
default-off**, so the agent launch path is byte-for-byte unchanged unless the
operator opts in:

  - ``AIFACTORY_ACT_GUARDRAIL``     → #474 anti-loop guardrail (guardrails.py)
  - ``AIFACTORY_MUTATION_LEDGER``   → #476 checkpoint + mutation ledger (mutation_ledger.py)
  - ``AIFACTORY_CONTEXT_SUMMARY``   → #475 PreCompact structured summary (context_summary.py)

Per-run state (the guardrail controller, the ledger, pending checkpoints) is held
in a registry keyed by the worktree ``cwd`` — every SDK hook input carries
``cwd``, and AIFactory runs one worktree per task, so this cleanly separates
parallel workers. ``register_session(cwd, spec_dir)`` is called from
``core.client.create_client``; the hooks no-op gracefully if a session was never
registered. Never raises into the agent: a hook error degrades to "allow".
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context_summary import summary_enabled, write_active_context
from .guardrails import Decision, ToolCallGuardrailController
from .mutation_ledger import (
    MUTATING_TOOLS,
    MutationLedger,
    git_checkpoint,
    ledger_enabled,
    mutation_target,
)
from .test_evidence import gate_enabled, is_test_command, record_test_run

logger = logging.getLogger(__name__)

_HALT_FILE = "guardrail_halt.json"


def guardrail_enabled() -> bool:
    return (os.environ.get("AIFACTORY_ACT_GUARDRAIL") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass
class _Session:
    spec_dir: Path
    guardrail: ToolCallGuardrailController
    ledger: MutationLedger
    checkpoints: dict[str, str | None] = field(default_factory=dict)


_SESSIONS: dict[str, _Session] = {}


def register_session(cwd: Path | str, spec_dir: Path | str) -> None:
    """Bind a worktree to a fresh per-run guardrail controller + ledger."""
    key = str(Path(cwd).resolve())
    _SESSIONS[key] = _Session(
        spec_dir=Path(spec_dir),
        guardrail=ToolCallGuardrailController(),
        ledger=MutationLedger(Path(spec_dir)),
    )


def _sess(input_data: dict[str, Any]) -> _Session | None:
    cwd = input_data.get("cwd")
    if not cwd:
        return None
    return _SESSIONS.get(str(Path(cwd).resolve()))


def _ok(tool_response: Any) -> bool:
    """Heuristic success: SDK tool errors surface as ``is_error`` dicts or an
    error-prefixed string. Anything else is treated as a success."""
    if isinstance(tool_response, dict):
        return not (tool_response.get("is_error") or tool_response.get("isError"))
    if isinstance(tool_response, str):
        return (
            not tool_response.lstrip()
            .lower()
            .startswith(("error", "traceback", "exception"))
        )
    return True


def _write_halt(spec_dir: Path, reason: str) -> None:
    try:
        (spec_dir / _HALT_FILE).write_text(
            json.dumps({"halt_reason": reason, "ts": time.time()})
        )
    except OSError:
        pass


def read_halt_reason(spec_dir: Path | str) -> str | None:
    """Read a guardrail halt reason recorded this run (for the Act loop to break
    early + stamp the completion event). None if no halt."""
    try:
        reason = json.loads((Path(spec_dir) / _HALT_FILE).read_text()).get(
            "halt_reason"
        )
        return str(reason) if reason is not None else None
    except (OSError, ValueError):
        return None


# ── #474 anti-loop guardrail ─────────────────────────────────────────────────


async def guardrail_pretool_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,  # noqa: ARG001 — SDK hook contract
    context: Any = None,  # noqa: ARG001 — SDK hook contract
) -> dict[str, Any]:
    if not guardrail_enabled():
        return {}
    try:
        s = _sess(input_data)
        if not s:
            return {}
        v = s.guardrail.before_call(
            input_data.get("tool_name", ""), input_data.get("tool_input")
        )
        if v.decision is Decision.HALT:
            _write_halt(s.spec_dir, v.reason or "no progress")
        if v.stop:
            return {"decision": "block", "reason": v.reason or "guardrail: no progress"}
    except Exception:  # noqa: BLE001 — never break the agent on a guard error
        logger.debug("guardrail pretool hook failed", exc_info=True)
    return {}


async def guardrail_posttool_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,  # noqa: ARG001 — SDK hook contract
    context: Any = None,  # noqa: ARG001 — SDK hook contract
) -> dict[str, Any]:
    if not guardrail_enabled():
        return {}
    try:
        s = _sess(input_data)
        if not s:
            return {}
        v = s.guardrail.after_call(
            input_data.get("tool_name", ""),
            input_data.get("tool_input"),
            ok=_ok(input_data.get("tool_response")),
            result=input_data.get("tool_response"),
        )
        if v.decision is Decision.HALT:
            _write_halt(s.spec_dir, v.reason or "no progress")
    except Exception:  # noqa: BLE001
        logger.debug("guardrail posttool hook failed", exc_info=True)
    return {}


# ── #476 checkpoint + mutation ledger ────────────────────────────────────────


async def mutation_pretool_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,
    context: Any = None,  # noqa: ARG001 — SDK hook contract
) -> dict[str, Any]:
    if not ledger_enabled():
        return {}
    try:
        s = _sess(input_data)
        tool = input_data.get("tool_name", "")
        if not s or tool not in MUTATING_TOOLS:
            return {}
        cwd = Path(input_data["cwd"])
        s.checkpoints[tool_use_id or ""] = git_checkpoint(cwd)
    except Exception:  # noqa: BLE001
        logger.debug("mutation pretool hook failed", exc_info=True)
    return {}


async def mutation_posttool_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,
    context: Any = None,
) -> dict[str, Any]:
    # #851: record test-command runs on EVERY post-tool event, independent of the
    # mutation ledger's own flag. Piggybacks this already-registered, always-on
    # post-hook so the evidence gate needs no new HookMatcher (and no edit to the
    # SDK-options builder in core/client.py).
    await test_evidence_posttool_hook(input_data, tool_use_id, context)
    if not ledger_enabled():
        return {}
    try:
        s = _sess(input_data)
        tool = input_data.get("tool_name", "")
        if not s or tool not in MUTATING_TOOLS:
            return {}
        s.ledger.record(
            tool=tool,
            target=mutation_target(tool, input_data.get("tool_input")),
            ok=_ok(input_data.get("tool_response")),
            checkpoint=s.checkpoints.get(tool_use_id or ""),
            tool_use_id=tool_use_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("mutation posttool hook failed", exc_info=True)
    return {}


# ── #851 test-execution evidence ─────────────────────────────────────────────


async def test_evidence_posttool_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,  # noqa: ARG001 — SDK hook contract; unused here
    context: Any = None,  # noqa: ARG001 — SDK hook contract; unused here
) -> dict[str, Any]:
    """Record every real test-command Bash run for the honest-verification gate.

    Tamper-evident: it captures the ACTUAL Bash execution, not the model's
    self-report. ``update_subtask_status`` then refuses to complete a
    test/verification subtask with no recorded run (#851). Never raises into the
    agent; a recording failure just leaves the gate to fall back to "no run".
    """
    if not gate_enabled():
        return {}
    try:
        if input_data.get("tool_name", "") != "Bash":
            return {}
        s = _sess(input_data)
        if not s:
            return {}
        tool_input = input_data.get("tool_input") or {}
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if command and is_test_command(command):
            record_test_run(s.spec_dir, command, input_data.get("tool_response"))
    except Exception:  # noqa: BLE001 — never break the agent on a recording error
        logger.debug("test-evidence posttool hook failed", exc_info=True)
    return {}


# ── #475 PreCompact structured summary ───────────────────────────────────────


async def precompact_summary_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None = None,  # noqa: ARG001 — SDK hook contract
    context: Any = None,  # noqa: ARG001 — SDK hook contract
) -> dict[str, Any]:
    if not summary_enabled():
        return {}
    try:
        s = _sess(input_data)
        if s:
            write_active_context(s.spec_dir)
            logger.info(
                "PreCompact (%s): refreshed active_context.md for re-anchor",
                input_data.get("trigger"),
            )
    except Exception:  # noqa: BLE001
        logger.debug("precompact summary hook failed", exc_info=True)
    return {}
