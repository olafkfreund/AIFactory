"""RFC-0001 completion-event emitter (AIFactory side).

When an AIFactory task reaches a terminal state, emit the normalized completion
event so the cockpit (CFactory) can thread the unit of work end to end:
``pfactory.session_id -> issue# -> aifactory.task_id``.

Conforms to the Factory correlation-key RFC (olafkfreund/Factory#4 / RFC-0001):
``{correlation_key, service, task_id, status, phase, updated_at}`` + an optional
``correlation`` chain block. The shared key is the source GitHub issue number
(rendered as a string), with a synthetic ``af-<spec_id>`` fallback so it is never
null.

Transport mirrors the other services: an opt-in webhook POST is the standardized
transport, and an opt-in same-host sentinel file is a convenience. Both are
best-effort — every failure is swallowed so a notification can never break the
pipeline. Stdlib-only.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "aifactory"
_SCHEMA_VERSION = "1.1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def read_issue_number(spec_dir: Path) -> int | None:
    """The source GitHub issue number from the task's requirements.json (set at
    task creation from a PFactory-emitted issue). None until a run carries one."""
    try:
        reqs = json.loads((spec_dir / "requirements.json").read_text())
    except (OSError, ValueError):
        return None
    raw = None
    if isinstance(reqs.get("metadata"), dict):
        raw = reqs["metadata"].get("githubIssueNumber")
    if not raw and isinstance(reqs.get("githubIssue"), dict):
        raw = reqs["githubIssue"].get("number")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def correlation_key(spec_id: str, issue_number: int | None) -> str:
    """RFC-0001 §2: issue# as a string, synthetic ``af-<spec_id>`` fallback."""
    return str(issue_number) if issue_number is not None else f"af-{spec_id}"


def read_usage(spec_dir: Path) -> dict | None:
    """The RFC-0001 v1.1 ``usage`` block from the task's ``token_usage.json``.

    The agent's token attribution writes ``<spec_dir>/token_usage.json`` with the
    run's aggregated token counts and real SDK cost. We map those fields to the
    RFC block. Returns ``None`` when there is no usage to report (file missing,
    unreadable, or zero tokens) — the block is additive, so it is simply omitted.
    Stdlib-only and best-effort, like the rest of this module.
    """
    try:
        agg = json.loads((spec_dir / "token_usage.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(agg, dict):
        return None
    in_tok = int(agg.get("totalInputTokens", 0) or 0)
    out_tok = int(agg.get("outputTokens", 0) or 0)
    if in_tok == 0 and out_tok == 0:
        return None
    total = int(agg.get("totalTokens", 0) or 0) or (in_tok + out_tok)
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total,
        "cost_usd": round(float(agg.get("totalCostUsd", 0.0) or 0.0), 6),
        "model": agg.get("model"),
    }


def build_completion_event(
    *,
    task_id: str,
    spec_id: str,
    status: str,
    issue_number: int | None,
    phase: str = "act",
    project_id: str | None = None,
    updated_at: str | None = None,
    usage: dict | None = None,
) -> dict:
    """The RFC-0001 completion-event envelope (six core fields + chain block).

    When ``usage`` is supplied (RFC-0001 v1.1 §3.1) it rides along so the cockpit
    can attribute the Code stage's token spend; additive and omitted otherwise.
    """
    event = {
        "correlation_key": correlation_key(spec_id, issue_number),
        "service": SERVICE_NAME,
        "task_id": task_id,
        "status": status,
        "phase": phase,
        "updated_at": updated_at or _now_iso(),
        "correlation": {
            "issue_number": issue_number,
            "spec_id": spec_id,
            "project_id": project_id,
        },
        # Additive (RFC §7): retained for parity with the other services.
        "schema_version": _SCHEMA_VERSION,
        "event": "completion",
    }
    if usage is not None:
        event["usage"] = usage
    return event


def _webhook_url() -> str | None:
    return (os.environ.get("AIFACTORY_COMPLETION_WEBHOOK") or "").strip() or None


def _sentinel_enabled() -> bool:
    return _truthy(os.environ.get("AIFACTORY_COMPLETION_SENTINEL"))


def notify_completion(event: dict, *, spec_dir: Path | None = None) -> None:
    """Best-effort terminal notification: opt-in sentinel + opt-in webhook POST.
    Never raises — a failing target must not break the pipeline."""
    if _sentinel_enabled() and spec_dir is not None:
        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "COMPLETED.json").write_text(json.dumps(event, indent=2))
        except OSError:
            pass

    url = _webhook_url()
    if not url:
        return
    try:
        import urllib.request

        timeout = float(os.environ.get("AIFACTORY_COMPLETION_WEBHOOK_TIMEOUT", "5"))
        req = urllib.request.Request(
            url,
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout).close()  # noqa: S310
    except Exception:
        logger.debug("completion webhook failed (best-effort)", exc_info=True)


def emit_terminal_completion(
    spec_dir: Path, *, task_id: str, project_id: str, spec_id: str, status: str,
) -> dict:
    """Build + emit the completion event for a task that reached ``status``.
    Returns the event (for callers/tests). Best-effort; never raises."""
    event = build_completion_event(
        task_id=task_id,
        spec_id=spec_id,
        status=status,
        issue_number=read_issue_number(spec_dir),
        project_id=project_id,
        usage=read_usage(spec_dir),
    )
    notify_completion(event, spec_dir=spec_dir)
    return event
