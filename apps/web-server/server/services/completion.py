"""RFC-0001 completion-event emitter (AIFactory side).

When an AIFactory task reaches a terminal state, emit the normalized completion
event so the cockpit (CFactory) can thread the unit of work end to end:
``pfactory.session_id -> issue# -> aifactory.task_id``.

Conforms to the Factory correlation-key RFC (olafkfreund/Factory#4 / RFC-0001):
``{correlation_key, service, task_id, status, phase, updated_at}`` + an optional
``correlation`` chain block. The shared key is the source GitHub issue number
(rendered as a string), with a synthetic ``af-<spec_id>`` fallback so it is never
null.

Additive envelope upgrade (#466) — every field below rides *alongside* the
existing ones; nothing is removed, so existing consumers (CFactory) keep working
unchanged until the cross-repo cutover:

  - ``id`` — a per-event UUIDv4 for consumer-side exactly-once dedup. It is
    *stable across retries of the same event*: the value is fixed once the event
    is built, and the outbox relay (#465) re-delivers the persisted row verbatim.
  - CloudEvents-core fields (``specversion``, ``source``, ``type``, ``time``) —
    align the bespoke envelope with CNCF CloudEvents 1.0 without restructuring.
  - ``traceparent`` / ``tracestate`` (W3C trace context) — OpenTelemetry
    correlation across the Factory pipeline.

Transport mirrors the other services: an opt-in webhook POST is the standardized
transport, and an opt-in same-host sentinel file is a convenience. Both are
best-effort — every failure is swallowed so a notification can never break the
pipeline. Stdlib-only.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "aifactory"
# Envelope grew additively in #466 (CloudEvents-core + id + trace context).
# v1.3 (#45 P1) adds an additive per-worker breakdown to the usage block:
# ``usage.workers[]`` + ``usage.by_provider{}`` / ``usage.by_model{}`` rollups.
# The scalar usage fields are KEPT verbatim, so old consumers ignore the new
# fields and still validate — an additive minor bump.
_SCHEMA_VERSION = "1.3"

# CloudEvents 1.0 (CNCF) — the spec version we align to, and the reverse-DNS
# ``type`` for AIFactory's terminal completion event. ``source`` is overridable
# per deployment so a multi-instance fleet can be told apart by the consumer.
_CE_SPECVERSION = "1.0"
_CE_TYPE = "io.factory.aifactory.completion"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ce_source() -> str:
    """CloudEvents ``source`` URI-reference identifying this producer."""
    return (os.environ.get("AIFACTORY_EVENT_SOURCE") or "/aifactory").strip() or "/aifactory"


def _new_event_id() -> str:
    """A fresh per-event idempotency id (CloudEvents ``id``)."""
    return str(uuid.uuid4())


def _new_traceparent() -> str:
    """A valid W3C ``traceparent`` (version 00, sampled flag set).

    ``00-<16-byte trace-id>-<8-byte span-id>-01`` — a freshly rooted trace for
    this terminal event when no inbound context is being propagated.
    """
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return f"00-{trace_id}-{span_id}-01"


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


def _worker_records(agg: dict) -> list[dict]:
    """Normalise the persisted per-worker map (#45 P1) into a stable list.

    ``token_usage.json`` carries a ``workers`` map keyed by worker_id; the RFC
    event exposes it as an ordered list of records. Each record echoes the
    on-disk shape. Returns ``[]`` when no worker breakdown was recorded (old
    files / nothing to report) — the field is additive and simply omitted then.
    """
    workers = agg.get("workers")
    if not isinstance(workers, dict) or not workers:
        return []
    records: list[dict] = []
    for wid, rec in workers.items():
        if not isinstance(rec, dict):
            continue
        in_tok = int(rec.get("input_tokens", 0) or 0)
        out_tok = int(rec.get("output_tokens", 0) or 0)
        records.append(
            {
                "worker_id": rec.get("worker_id") or wid,
                "phase": rec.get("phase"),
                "subtask_id": rec.get("subtask_id"),
                "provider": rec.get("provider"),
                "model": rec.get("model"),
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": int(rec.get("total_tokens", 0) or 0)
                or (in_tok + out_tok),
                "cost_usd": round(float(rec.get("cost_usd", 0.0) or 0.0), 6),
                "duration_ms": int(rec.get("duration_ms", 0) or 0),
            }
        )
    # Deterministic order for stable events/tests.
    records.sort(key=lambda r: str(r["worker_id"]))
    return records


def _rollup(records: list[dict], key: str) -> dict:
    """Roll worker records up by ``provider`` or ``model`` into a token/cost map.

    Records with a null/empty key are bucketed under ``"unknown"`` so the rollup
    never silently drops a worker's spend.
    """
    out: dict[str, dict] = {}
    for rec in records:
        bucket = rec.get(key) or "unknown"
        slot = out.setdefault(
            bucket,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "workers": 0,
            },
        )
        slot["input_tokens"] += int(rec.get("input_tokens", 0) or 0)
        slot["output_tokens"] += int(rec.get("output_tokens", 0) or 0)
        slot["total_tokens"] += int(rec.get("total_tokens", 0) or 0)
        slot["cost_usd"] = round(slot["cost_usd"] + float(rec.get("cost_usd", 0.0) or 0.0), 6)
        slot["workers"] += 1
    return out


def read_usage(spec_dir: Path) -> dict | None:
    """The RFC-0001 ``usage`` block from the task's ``token_usage.json``.

    The agent's token attribution writes ``<spec_dir>/token_usage.json`` with the
    run's aggregated token counts and real SDK cost. We map those fields to the
    RFC block. Returns ``None`` when there is no usage to report (file missing,
    unreadable, or zero tokens) — the block is additive, so it is simply omitted.
    Stdlib-only and best-effort, like the rest of this module.

    v1.3 (#45 P1, additive): when the file carries a per-worker ``workers`` map,
    the block also gains ``workers[]`` + ``by_provider{}`` / ``by_model{}``
    rollups derived from it. The scalar fields above are KEPT verbatim for
    back-compat; a serial single-model task still produces exactly the same
    scalar block plus a one-entry workers list.
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
    block = {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": total,
        "cost_usd": round(float(agg.get("totalCostUsd", 0.0) or 0.0), 6),
        "model": agg.get("model"),
    }
    workers = _worker_records(agg)
    if workers:
        block["workers"] = workers
        block["by_provider"] = _rollup(workers, "provider")
        block["by_model"] = _rollup(workers, "model")
    return block


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
    event_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
    halt_reason: str | None = None,
) -> dict:
    """The RFC-0001 completion-event envelope (six core fields + chain block).

    When ``usage`` is supplied (RFC-0001 v1.1 §3.1) it rides along so the cockpit
    can attribute the Code stage's token spend; additive and omitted otherwise.

    Additive #466 fields ride alongside the legacy ones:

      - ``event_id`` — pass an explicit id to make a rebuild reproduce the *same*
        event (so the relay's re-delivery dedups). Defaults to a fresh UUIDv4.
      - ``traceparent``/``tracestate`` — propagate inbound W3C trace context, or
        let a fresh ``traceparent`` be rooted here. ``tracestate`` is omitted
        unless supplied.
    """
    when = updated_at or _now_iso()
    event = {
        "correlation_key": correlation_key(spec_id, issue_number),
        "service": SERVICE_NAME,
        "task_id": task_id,
        "status": status,
        "phase": phase,
        "updated_at": when,
        "correlation": {
            "issue_number": issue_number,
            "spec_id": spec_id,
            "project_id": project_id,
        },
        # Additive (RFC §7): retained for parity with the other services.
        "schema_version": _SCHEMA_VERSION,
        "event": "completion",
        # Per-event idempotency key (#466) — consumers dedup on this; stable
        # across relay re-delivery because the built event is persisted verbatim.
        "id": event_id or _new_event_id(),
        # CloudEvents-core alignment (#466) — additive siblings of the legacy
        # fields above; ``time`` mirrors the occurrence time (``updated_at``).
        "specversion": _CE_SPECVERSION,
        "source": _ce_source(),
        "type": _CE_TYPE,
        "time": when,
        # W3C trace context (#466) for OpenTelemetry correlation.
        "traceparent": traceparent or _new_traceparent(),
    }
    if tracestate:
        event["tracestate"] = tracestate
    if usage is not None:
        event["usage"] = usage
    # Anti-loop guardrail (#474): when the Act loop halted on no-progress, carry
    # the typed reason so CFactory can show *why* a WorkItem stalled.
    if halt_reason:
        event["halt_reason"] = halt_reason
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

    # At-least-once path (#465): when the outbox flag is on, persist the event
    # durably and let the retrying relay deliver it — a crash before delivery no
    # longer loses the event. Off by default, so the direct POST below is the
    # untouched legacy path until the relay is verified and cut over (#468).
    try:
        from .outbox import enqueue, outbox_enabled

        if outbox_enabled():
            enqueue(event, url)
            return
    except Exception:  # noqa: BLE001 — fall back to the direct POST, never raise
        logger.debug("outbox enqueue failed; falling back to direct POST", exc_info=True)

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
        halt_reason=_read_halt_reason(spec_dir),
    )
    notify_completion(event, spec_dir=spec_dir)
    return event


def _read_halt_reason(spec_dir: Path) -> str | None:
    """The Act-loop guardrail's typed no-progress reason for this run, if any
    (#474). Written by ``agents.act_loop_hooks`` to ``guardrail_halt.json``."""
    try:
        data = json.loads((spec_dir / "guardrail_halt.json").read_text())
        reason = data.get("halt_reason")
        return reason if isinstance(reason, str) else None
    except (OSError, ValueError):
        return None
