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
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory_common.logsafe import sanitize_log

from .billing import classify_billing_mode

logger = logging.getLogger(__name__)

SERVICE_NAME = "aifactory"
# CloudEvents 1.0 (CNCF) — the spec version we align to, and the reverse-DNS
# ``type`` for AIFactory's terminal completion event. ``source`` is overridable
# per deployment so a multi-instance fleet can be told apart by the consumer.
_CE_SPECVERSION = "1.0"
_CE_TYPE = "io.factory.aifactory.completion"
# Live per-worker sub-event (#45 P1): a distinct CloudEvents ``type`` so a
# consumer can route the live drill-down events apart from the terminal rollup.
_CE_TYPE_WORKER = "io.factory.aifactory.worker"
# Status + phase of the live sub-event each worker emits as it finishes.
_WORKER_STATUS = "worker_done"
_WORKER_PHASE = "worker"

# Live per-worker PROGRESS heartbeat (#45 P2 Tier 2): a periodic "still running"
# event emitted from the per-worker run path while a worker is in flight, so the
# cockpit can render a per-second-ticking graph for a running task. Distinct from
# the one-shot ``worker_done`` event above: a worker emits MANY progress
# heartbeats while running and exactly one ``worker_done`` when it finishes.
_CE_TYPE_WORKER_PROGRESS = "io.factory.aifactory.worker_progress"
_WORKER_PROGRESS_STATUS = "running"
_WORKER_PROGRESS_PHASE = "worker_progress"

# Heartbeat throttle window (seconds). At most one progress event per worker_id
# per window, so a fast per-turn loop can't flood the transport — the heartbeat
# is a bounded ~1-point-per-window signal, not a per-turn firehose. Overridable
# for tests/ops via AIFACTORY_WORKER_PROGRESS_INTERVAL_S.
_WORKER_PROGRESS_WINDOW_S = 10.0

# Per-worker_id monotonic timestamp of the last progress emit. Module-level
# state: one running build is one subprocess, so this map is naturally scoped to
# a single task's lifetime. ``{worker_id: last_emit_monotonic}``.
_progress_last_emit: dict[str, float] = {}


def _pool_memory_at_project_level(spec_dir: str | os.PathLike[str]) -> bool:
    """Pool a spec's memory into the PROJECT store, so it compounds (#1038).

    Runs in the CONTROL PLANE, right after the packed-path fetch, because this is
    the first moment the memory exists on a durable filesystem. The build process
    cannot do it: on the packed path every path it can see is under ``/work``, an
    emptyDir that dies with the pod.

    ``spec_dir`` is ``<project>/.aifactory/specs/<id>``, so the project's
    ``.aifactory/`` is its grandparent and the store sits beside ``specs/``.

    **Why project level.** A project holds many specs (86 tasks in one live
    workspace) and each is built roughly once, so a spec-scoped store has almost
    nothing to read it — the next build is a different spec in a different
    directory. Memory is only worth keeping if a lesson from spec 034 reaches
    spec 041.

    MERGES, never replaces: the pool is the union of every spec's insights and no
    single spec may clear it. Best-effort — a build that produced working code
    must not fail because its memory could not be pooled.
    """
    src = Path(spec_dir).resolve() / "memory"
    if not src.is_dir():
        return False
    try:
        # The project's .aifactory/ is the spec dir's grandparent. Both sides are
        # resolved and the containment is CHECKED rather than assumed: spec_dir
        # originates from a request, and a "../.." that walked out of the project
        # would have this function copying a memory tree somewhere arbitrary.
        project_aifactory = src.parent.parent.parent
        dest = (project_aifactory / "memory").resolve()
        if not dest.is_relative_to(project_aifactory.resolve()):
            logger.warning("[completion] refusing to pool memory outside the project")
            return False
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        # The path is deliberately NOT interpolated: it derives from request data
        # and a log line is not the place to echo it back (py/log-injection).
        logger.info("[completion] pooled memory at project level")
        return True
    except (OSError, shutil.Error) as exc:
        logger.warning("[completion] could not pool memory: %s", exc)
        return False


def _progress_window_s() -> float:
    """The throttle window in seconds (env-overridable, defaults to ~10s)."""
    raw = os.environ.get("AIFACTORY_WORKER_PROGRESS_INTERVAL_S")
    if raw is None:
        return _WORKER_PROGRESS_WINDOW_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _WORKER_PROGRESS_WINDOW_S
    return val if val >= 0 else _WORKER_PROGRESS_WINDOW_S


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ce_source() -> str:
    """CloudEvents ``source`` URI-reference identifying this producer."""
    return (
        os.environ.get("AIFACTORY_EVENT_SOURCE") or "/aifactory"
    ).strip() or "/aifactory"


def _new_event_id() -> str:
    """A fresh per-event idempotency id (CloudEvents ``id``)."""
    return str(uuid.uuid4())


def _new_traceparent() -> str:
    """A valid W3C ``traceparent`` (version 00, sampled flag set).

    Prefer the CURRENTLY ACTIVE span's trace context (#45 P1): when this
    terminal event is built inside a request/task span (FastAPI auto-
    instrumentation has one open when OTel is enabled), reusing its
    ``traceparent`` LINKS the completion event to that trace instead of
    rooting an orphan. Falls back to a freshly rooted synthetic trace
    (``00-<16-byte trace-id>-<8-byte span-id>-01``) when no span is active —
    e.g. OTel disabled, or emitted outside a request context. Additive + safe:
    the return shape is unchanged and the fallback matches the old behavior.
    """
    try:
        from ..observability.tracing import get_current_traceparent

        active = get_current_traceparent()
        if active:
            return active
    except Exception:
        # Any import/lookup failure → fall through to the synthetic value.
        pass
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
        record = {
            "worker_id": rec.get("worker_id") or wid,
            "phase": rec.get("phase"),
            "subtask_id": rec.get("subtask_id"),
            "provider": rec.get("provider"),
            "model": rec.get("model"),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": int(rec.get("total_tokens", 0) or 0) or (in_tok + out_tok),
            "cost_usd": round(float(rec.get("cost_usd", 0.0) or 0.0), 6),
            "duration_ms": int(rec.get("duration_ms", 0) or 0),
            # Billing mode (#96): api/cloud are metered (show cost);
            # subscription/local are not (show tokens + time). Lets CFactory
            # avoid surfacing a notional dollar cost for subscription/local work.
            "billing_mode": classify_billing_mode(rec.get("provider")),
        }
        # RFC-0014 (#803, additive): echo the routing tier the backend's token
        # attribution stamped when a routing policy was active. Omitted entirely
        # when absent (policy off), keeping the envelope byte-identical to v1.3.
        if rec.get("routing_tier"):
            record["routing_tier"] = rec["routing_tier"]
        records.append(record)
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
        slot["cost_usd"] = round(
            slot["cost_usd"] + float(rec.get("cost_usd", 0.0) or 0.0), 6
        )
        slot["workers"] += 1
        slot["duration_ms"] = int(slot.get("duration_ms", 0)) + int(
            rec.get("duration_ms", 0) or 0
        )
        # A provider bucket shares one billing mode; carry it so CFactory knows
        # whether this bucket's spend is real dollars or notional (#96).
        if rec.get("billing_mode"):
            slot.setdefault("billing_mode", rec.get("billing_mode"))
    return out


def _read_tenant_id(spec_dir: Path) -> str | None:
    """The tenant stamped on the spec's ``task_metadata.json``, or ``None``.

    Local (not ``server.tenancy``) because this module is also exec'd standalone
    by ``agents/parallel_integration.py`` (spec_from_file_location), where a
    relative import would fail. Mirrors ``_read_budget_usd``: stdlib-only,
    best-effort, never raises. Multi-tenancy #925.
    """
    try:
        meta = json.loads((spec_dir / "task_metadata.json").read_text())
    except (OSError, ValueError):
        return None
    tenant = meta.get("tenant_id") if isinstance(meta, dict) else None
    return str(tenant) if tenant else None


def _read_budget_usd(spec_dir: Path) -> float | None:
    """The optional soft cost budget (USD) for this task, if set (#45 P2).

    Sourced from the Task Contract's ``execution.budget_usd``, which
    ``trusted_plan.apply_execution_profile`` carries into the spec's
    ``task_metadata.json`` as ``budgetUsd``. Returns ``None`` when no budget is
    set (file missing/unreadable, or the key absent/non-numeric) — the budget
    block is then omitted entirely (back-compat). Stdlib-only, best-effort,
    never raises.
    """
    try:
        meta = json.loads((spec_dir / "task_metadata.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("budgetUsd")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        budget = float(raw)
    except (TypeError, ValueError):
        return None
    return budget if budget >= 0 else None


def _budget_block(spent_usd: float, budget_usd: float | None) -> dict | None:
    """The additive ``usage.budget`` soft-alert block (#45 P2), or ``None``.

    OBSERVE-ONLY: this only describes the rolled-up spend against the contract
    budget; it never aborts, pauses, or kills the build. Returns ``None`` when no
    budget is set, so the block is omitted entirely (back-compat). EXACT shape
    (CFactory consumes it): ``{limit_usd, spent_usd, exceeded}``.
    """
    if budget_usd is None:
        return None
    spent = round(float(spent_usd or 0.0), 6)
    exceeded = spent > budget_usd
    if exceeded:
        # Fire the OTel counter (no-op when OTel disabled). NEVER aborts.
        _emit_budget_exceeded()
    return {
        "limit_usd": round(float(budget_usd), 6),
        "spent_usd": spent,
        "exceeded": exceeded,
    }


def _emit_budget_exceeded() -> None:
    """Increment the OTel ``budget.exceeded`` counter (#45 P2). Best-effort.

    Complete no-op when OTel is disabled; never raises. OBSERVE-ONLY — recording
    a metric only, with no effect on the build's terminal state."""
    try:
        from ..observability.metrics_otel import record_budget_exceeded

        record_budget_exceeded()
    except Exception:  # noqa: BLE001 — metrics must never break completion
        logger.debug("budget.exceeded metric emit failed (best-effort)", exc_info=True)


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
    return usage_from_aggregate(agg, spec_dir=spec_dir)


def usage_from_aggregate(
    agg: dict[str, Any] | None, *, spec_dir: Path | None = None
) -> dict[str, Any] | None:
    """The RFC-0001 ``usage`` block from an already-loaded aggregate.

    Split out of :func:`read_usage` (#1249) so the live path and the file path
    map the SAME way. Under the kubejob backend ``token_usage.json`` is written
    inside the Job's ephemeral ``/work`` and only pushed back at the END, so
    there is no file to read while the build runs; the live snapshot arrives as
    a ``__USAGE__`` marker on the log stream instead and is mapped here. Two
    copies of this mapping is how one of them goes stale — the same argument
    #1229 makes for phase parsing.
    """
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
        # OTel per-worker / per-provider metric emission (#45 P1). Complete
        # no-op when OTel is disabled (no exporter endpoint) and best-effort
        # otherwise — never affects the usage block or the completion path.
        _emit_worker_metrics(workers)
    # Soft budget alert (#45 P2, additive + OBSERVE-ONLY): when the contract set
    # a budget (carried into task_metadata.budgetUsd), attach a usage.budget
    # block comparing the rolled-up aggregate spend (block["cost_usd"]) against
    # it. Omitted entirely when no budget is set (back-compat). NEVER aborts the
    # build — this is purely a warning surface on the terminal event.
    #
    # spec_dir is optional (#1249): the live path maps a marker payload with no
    # spec dir in hand. No spec dir means no task_metadata.json to read, so the
    # budget block is omitted exactly as it is when no budget was set — which is
    # the documented back-compat behaviour, not a new silent gap.
    budget = (
        _budget_block(block["cost_usd"], _read_budget_usd(spec_dir))
        if spec_dir is not None
        else None
    )
    if budget is not None:
        block["budget"] = budget
    return block


def _emit_worker_metrics(workers: list[dict]) -> None:
    """Re-emit the per-worker breakdown as OTel metrics (#45 P1).

    The OTLP exporter lives in the web-server (the agent subprocess has none),
    so the worker records the agent persisted are exported from here at task
    end. A complete no-op when OTel is disabled; never raises.
    """
    try:
        from ..observability.metrics_otel import record_worker_metrics_batch

        record_worker_metrics_batch(workers)
    except Exception:  # noqa: BLE001 — metrics must never break completion
        logger.debug("worker metrics emit failed (best-effort)", exc_info=True)


def build_completion_event(
    *,
    task_id: str,
    spec_id: str,
    status: str,
    issue_number: int | None,
    phase: str = "act",
    project_id: str | None = None,
    updated_at: str | None = None,
    usage: dict[str, Any] | None = None,
    event_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
    halt_reason: str | None = None,
    injection_scan: dict | None = None,
    tenant_id: str | None = None,
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
    # RFC-0001a evidence gate: a build may only claim a SUCCESS status if it
    # carries proof it actually ran. A 0-token "completed" is a dead build — an
    # expired provider credential produces a stub plan that finishes in seconds
    # having consumed nothing, and was historically reported green. Downgrade it
    # to failed with a "no_evidence" reason so no consumer can render it as a
    # pass. Scoped to events that carry usage (i.e. build events); never touches
    # the legitimate FAILED path or non-build events.
    _SUCCESS_STATUSES = {"completed", "passed", "verified", "succeeded"}
    if status in _SUCCESS_STATUSES and usage is not None:
        _evidence_tokens = int((usage or {}).get("total_tokens", 0) or 0)
        if _evidence_tokens <= 0:
            status = "failed"
            halt_reason = (
                halt_reason or "no_evidence: build emitted 0 tokens (did not run)"
            )
    when = updated_at or _now_iso()
    event = {
        "correlation_key": correlation_key(spec_id, issue_number),
        "service": SERVICE_NAME,
        "task_id": task_id,
        "status": status,
        "phase": phase,
        "correlation": {
            "issue_number": issue_number,
            "spec_id": spec_id,
            "project_id": project_id,
        },
        # Per-event idempotency key (#466) — consumers dedup on this; stable
        # across relay re-delivery because the built event is persisted verbatim.
        "id": event_id or _new_event_id(),
        # CloudEvents-core (#466 envelope; #471 cutover removed the legacy
        # ``schema_version``/``event``/``updated_at`` duplicates). ``time`` is the
        # canonical occurrence time consumers read.
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
        # RFC-0001a evidence block: the proof this build stage ran. `tokens` is
        # the gate field (see the downgrade above); consumers MAY require it.
        event["evidence"] = {
            "proof_kind": "tokens",
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
            "cost_usd": float(usage.get("cost_usd", 0.0) or 0.0),
        }
    # Multi-tenancy (#925): OPTIONAL additive tenant scope — present only when
    # the spec carries a tenant stamp; old/single-tenant envelopes are unchanged.
    if tenant_id:
        event["tenant_id"] = tenant_id
    # Anti-loop guardrail (#474): when the Act loop halted on no-progress, carry
    # the typed reason so CFactory can show *why* a WorkItem stalled.
    if halt_reason:
        event["halt_reason"] = halt_reason
    # Pre-coder untrusted-content scan verdict (#805 / Factory#273): additive
    # block so CFactory can surface pass/flagged/skipped + matched patterns.
    if injection_scan:
        event["injection_scan"] = injection_scan
    return event


def build_worker_event(
    *,
    task_id: str,
    spec_id: str,
    issue_number: int | None,
    worker: dict,
    project_id: str | None = None,
    updated_at: str | None = None,
    event_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict:
    """A LIVE per-worker sub-event (RFC-0001 v1.3, #45 P1).

    Today only ONE terminal completion event fires at task end. This builds the
    additive *live* event each parallel worker (and the serial single 'main'
    worker) emits as it finishes, so the cockpit can show per-worker spend as it
    happens rather than only the end-of-task rollup. Shape mirrors
    :func:`build_completion_event` (same correlation key, same CloudEvents
    envelope) but carries ``status="worker_done"`` / ``phase="worker"`` and an
    additive top-level ``worker`` object instead of ``usage``.

    Dedup key for consumers: ``(service, correlation_key, worker.worker_id)`` —
    a worker emits at most one terminal sub-event, and a relay re-delivers the
    persisted event verbatim, so re-delivery dedups on the same triple.

    ``worker`` is the per-worker record already persisted to ``token_usage.json``
    (the agent's token attribution writes it); it is normalised here to the exact
    RFC field set, never recomputed.
    """
    when = updated_at or _now_iso()
    rec = worker or {}
    in_tok = int(rec.get("input_tokens", 0) or 0)
    out_tok = int(rec.get("output_tokens", 0) or 0)
    worker_block = {
        "worker_id": str(rec.get("worker_id") or "main"),
        "subtask_id": rec.get("subtask_id"),
        "agent_phase": rec.get("agent_phase") or rec.get("phase"),
        "provider": rec.get("provider"),
        "model": rec.get("model"),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": int(rec.get("total_tokens", 0) or 0) or (in_tok + out_tok),
        "cost_usd": round(float(rec.get("cost_usd", 0.0) or 0.0), 6),
        "duration_ms": int(rec.get("duration_ms", 0) or 0),
    }
    event = {
        "correlation_key": correlation_key(spec_id, issue_number),
        "service": SERVICE_NAME,
        "task_id": task_id,
        "status": _WORKER_STATUS,
        "phase": _WORKER_PHASE,
        "correlation": {
            "issue_number": issue_number,
            "spec_id": spec_id,
            "project_id": project_id,
        },
        # #471 cutover: legacy schema_version/event/updated_at dropped; ``time`` is
        # the canonical occurrence time.
        "id": event_id or _new_event_id(),
        "specversion": _CE_SPECVERSION,
        "source": _ce_source(),
        # Distinct CloudEvents ``type`` so the live sub-event is routable apart
        # from the terminal rollup; everything else matches the terminal envelope.
        "type": _CE_TYPE_WORKER,
        "time": when,
        "traceparent": traceparent or _new_traceparent(),
        "worker": worker_block,
    }
    if tracestate:
        event["tracestate"] = tracestate
    return event


def build_worker_progress_event(
    *,
    task_id: str,
    spec_id: str,
    issue_number: int | None,
    worker: dict,
    project_id: str | None = None,
    updated_at: str | None = None,
    event_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict:
    """A LIVE per-worker PROGRESS heartbeat event (#45 Tier 2).

    Sibling of :func:`build_worker_event`, but for a worker that is STILL
    RUNNING: it carries ``status="running"`` / ``phase="worker_progress"`` and a
    distinct CloudEvents ``type`` (``io.factory.aifactory.worker_progress``) so a
    consumer can stream a per-second-ticking graph of a live task and route these
    heartbeats apart from both the terminal rollup and the one-shot
    ``worker_done`` event. A worker emits MANY of these while running (throttled
    — see :func:`emit_worker_progress`) and exactly one ``worker_done`` when it
    finishes; the ``worker_done`` / terminal events are unchanged.

    The ``worker`` object is the running snapshot the per-worker run path holds
    (cumulative ``total_tokens`` / ``cost_usd`` and ``elapsed_ms`` since the
    worker started). Shape mirrors the EXACT v1.3 heartbeat contract a future
    CFactory consumer matches against.
    """
    when = updated_at or _now_iso()
    rec = worker or {}
    in_tok = int(rec.get("input_tokens", 0) or 0)
    out_tok = int(rec.get("output_tokens", 0) or 0)
    worker_block = {
        "worker_id": str(rec.get("worker_id") or "main"),
        "subtask_id": rec.get("subtask_id"),
        "agent_phase": rec.get("agent_phase") or rec.get("phase"),
        "provider": rec.get("provider"),
        "model": rec.get("model"),
        "total_tokens": int(rec.get("total_tokens", 0) or 0) or (in_tok + out_tok),
        "cost_usd": round(float(rec.get("cost_usd", 0.0) or 0.0), 6),
        "elapsed_ms": int(rec.get("elapsed_ms", 0) or 0),
    }
    event = {
        "correlation_key": correlation_key(spec_id, issue_number),
        "service": SERVICE_NAME,
        "task_id": task_id,
        "status": _WORKER_PROGRESS_STATUS,
        "phase": _WORKER_PROGRESS_PHASE,
        "correlation": {
            "issue_number": issue_number,
            "spec_id": spec_id,
            "project_id": project_id,
        },
        # #471 cutover: legacy schema_version/event/updated_at dropped; ``time`` is
        # the canonical occurrence time.
        "id": event_id or _new_event_id(),
        "specversion": _CE_SPECVERSION,
        "source": _ce_source(),
        "type": _CE_TYPE_WORKER_PROGRESS,
        "time": when,
        "traceparent": traceparent or _new_traceparent(),
        "worker": worker_block,
    }
    if tracestate:
        event["tracestate"] = tracestate
    return event


def emit_worker_completion(
    *,
    spec_dir: Path,
    task_id: str,
    spec_id: str,
    project_id: str | None,
    worker: dict,
) -> dict | None:
    """Build + best-effort emit one LIVE per-worker sub-event (#45 P1).

    Reuses the SAME transport as the terminal event (:func:`notify_completion`
    — opt-in webhook + opt-in sentinel, both swallow every error). A failing or
    slow emit must NEVER fail or slow the build, so the whole call is wrapped:
    any exception is logged at debug and discarded, exactly like the terminal
    path's contract. Returns the event (for callers/tests) or ``None`` if it
    could not be built.

    The sentinel for a worker sub-event is written to a per-worker filename so it
    never clobbers the terminal ``COMPLETED.json``.
    """
    try:
        event = build_worker_event(
            task_id=task_id,
            spec_id=spec_id,
            issue_number=read_issue_number(spec_dir),
            project_id=project_id,
            worker=worker,
        )
        _notify_worker(event, spec_dir=spec_dir)
        return event
    except Exception:  # noqa: BLE001 — a live sub-event must never break a build
        logger.debug("worker sub-event emit failed (best-effort)", exc_info=True)
        return None


def _notify_worker(event: dict, *, spec_dir: Path | None) -> None:
    """Best-effort live sub-event transport: opt-in sentinel + opt-in webhook.

    Mirrors :func:`notify_completion` (same env-gated webhook + sentinel), but
    the sentinel lands in a per-worker file so concurrent workers and the
    terminal ``COMPLETED.json`` never collide. Never raises."""
    if _sentinel_enabled() and spec_dir is not None:
        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
            wid = str((event.get("worker") or {}).get("worker_id") or "main")
            safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in wid)
            (spec_dir / f"WORKER.{safe}.json").write_text(json.dumps(event, indent=2))
        except OSError as e:
            logger.warning(
                "Failed to write worker sentinel under %s: %s",
                sanitize_log(spec_dir),
                sanitize_log(e),
            )

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
        logger.debug("worker sub-event webhook failed (best-effort)", exc_info=True)


def emit_worker_progress(
    *,
    spec_dir: Path,
    task_id: str,
    spec_id: str,
    project_id: str | None,
    worker: dict,
) -> dict | None:
    """Build + best-effort emit ONE throttled per-worker PROGRESS heartbeat
    (#45 Tier 2).

    THROTTLED: at most one heartbeat per ``worker_id`` per window (~10s, see
    :data:`_WORKER_PROGRESS_WINDOW_S`). Repeated calls inside the window are
    skipped and return ``None`` — so a fast per-turn build loop produces a
    bounded ~1-point-per-window heartbeat, never a flood. The throttle is keyed
    on a monotonic clock so it's wall-clock-jump safe.

    Best-effort + non-blocking by contract: a missing/slow/raising transport is
    swallowed (mirrors :func:`emit_worker_completion`). It must NEVER fail, slow,
    or block the build. Returns the emitted event, or ``None`` when throttled or
    on any failure.

    Transport reuses the live sub-event path (:func:`_notify_worker_progress`),
    but the sentinel is a ROLLING ``WORKER.<id>.progress.json`` file — it is
    overwritten on each heartbeat and never clobbers the terminal
    ``COMPLETED.json`` or the worker's final ``WORKER.<id>.json``.
    """
    try:
        wid = str((worker or {}).get("worker_id") or "main")
        now = time.monotonic()
        window = _progress_window_s()
        last = _progress_last_emit.get(wid)
        if last is not None and (now - last) < window:
            return None  # throttled — within the per-worker window
        # Reserve the slot BEFORE emitting so a slow transport can't let a
        # second concurrent call slip through the window.
        _progress_last_emit[wid] = now
        event = build_worker_progress_event(
            task_id=task_id,
            spec_id=spec_id,
            issue_number=read_issue_number(spec_dir),
            project_id=project_id,
            worker=worker,
        )
        _notify_worker_progress(event, spec_dir=spec_dir)
        return event
    except Exception:  # noqa: BLE001 — a progress heartbeat must never break a build
        logger.debug(
            "worker progress heartbeat emit failed (best-effort)", exc_info=True
        )
        return None


def _notify_worker_progress(event: dict, *, spec_dir: Path | None) -> None:
    """Best-effort heartbeat transport: opt-in rolling sentinel + opt-in webhook.

    Mirrors :func:`_notify_worker`, but the sentinel is a ROLLING
    ``WORKER.<id>.progress.json`` (overwritten each heartbeat) so it never
    collides with the worker's final ``WORKER.<id>.json`` or the terminal
    ``COMPLETED.json``. Never raises."""
    if _sentinel_enabled() and spec_dir is not None:
        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
            wid = str((event.get("worker") or {}).get("worker_id") or "main")
            safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in wid)
            (spec_dir / f"WORKER.{safe}.progress.json").write_text(
                json.dumps(event, indent=2)
            )
        except OSError as e:
            logger.warning(
                "Failed to write worker progress sentinel under %s: %s",
                sanitize_log(spec_dir),
                sanitize_log(e),
            )

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
        logger.debug("worker progress webhook failed (best-effort)", exc_info=True)


def _webhook_url() -> str | None:
    return (os.environ.get("AIFACTORY_COMPLETION_WEBHOOK") or "").strip() or None


def _sentinel_enabled() -> bool:
    return _truthy(os.environ.get("AIFACTORY_COMPLETION_SENTINEL"))


def notify_completion(event: dict, *, spec_dir: Path | None = None) -> bool:
    """Best-effort terminal notification: opt-in sentinel + opt-in webhook POST.

    Never raises — a failing target must not break the pipeline. Returns whether
    the event reached its destination, so a caller writing a fire-once marker can
    record "delivered" rather than "attempted" (#1407).

    True also when there is no webhook configured and when the outbox accepted
    the event: in both cases this function did everything asked of it. False
    means the POST was tried and failed.
    """
    if _sentinel_enabled() and spec_dir is not None:
        try:
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "COMPLETED.json").write_text(json.dumps(event, indent=2))
        except OSError as e:
            logger.warning(
                "Failed to write COMPLETED.json under %s: %s",
                sanitize_log(spec_dir),
                sanitize_log(e),
            )

    url = _webhook_url()
    if not url:
        return True

    # At-least-once path (#465): when the outbox flag is on, persist the event
    # durably and let the retrying relay deliver it — a crash before delivery no
    # longer loses the event. Off by default, so the direct POST below is the
    # untouched legacy path until the relay is verified and cut over (#468).
    try:
        from .outbox import enqueue, outbox_enabled

        if outbox_enabled():
            enqueue(event, url)
            return True
    except Exception:  # noqa: BLE001 — fall back to the direct POST, never raise
        logger.debug(
            "outbox enqueue failed; falling back to direct POST", exc_info=True
        )

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
        # WARNING, not debug (#1407). This ran at debug in a pod logging at
        # info, so a webhook that never delivered produced no line anywhere:
        # 12,492 log lines over 24h contained "webhook" zero times while the
        # cockpit showed $0.00 for every run. A best-effort side effect that
        # silently never happens is not best-effort, it is off.
        logger.warning(
            "completion webhook POST to %s failed; CFactory will not receive "
            "this task's usage",
            sanitize_log(url),
            exc_info=True,
        )
        return False
    return True


def emit_terminal_completion(
    spec_dir: Path,
    *,
    task_id: str,
    project_id: str,
    spec_id: str,
    status: str,
) -> dict:
    """Build + emit the completion event for a task that reached ``status``.
    Returns the event (for callers/tests). Best-effort; never raises."""
    # RFC-0017 #190: on the packed multi-node path the build Job wrote
    # token_usage.json into its ephemeral /work, not this control-plane data-PVC
    # spec dir — so fetch the copy the Job pushed to object storage before reading
    # it, else CFactory shows zero token usage. No-op on the co-mount path (file
    # already present) and best-effort (never raises).
    try:
        from core.workspace_fetch import (  # noqa: PLC0415
            maybe_fetch_memory,
            maybe_fetch_plan,
            maybe_fetch_task_logs,
            maybe_fetch_usage,
        )

        maybe_fetch_usage(spec_dir, spec_id)
        # W1 (Factory #218): also pull the Job's task_logs.json so spec_to_task
        # reports the real terminal status (done/failed) on the packed path
        # instead of defaulting to backlog/queued.
        maybe_fetch_task_logs(spec_dir, spec_id)
        # #852: and the plan, whose subtask statuses decide build_succeeded. The
        # control plane's copy is the pre-dispatch original (all pending), so
        # without this every successful packed build counts 0 completed subtasks
        # and escalates to human_review — blocking the TFactory handoff behind a
        # bookkeeping gap (Factory#253 "output propagation").
        maybe_fetch_plan(spec_dir, spec_id)
        # #1038: and the memory tree, which otherwise dies in the Job's emptyDir.
        # Fetched BEFORE pooling below, since the pool is built from this copy.
        maybe_fetch_memory(spec_dir, spec_id)
        _pool_memory_at_project_level(spec_dir)
    except Exception:  # noqa: BLE001 - defensive; emit must still proceed
        logger.debug("usage fetch skipped (best-effort)", exc_info=True)
    usage = read_usage(spec_dir)
    event = build_completion_event(
        task_id=task_id,
        spec_id=spec_id,
        status=status,
        issue_number=read_issue_number(spec_dir),
        project_id=project_id,
        usage=usage,
        halt_reason=_read_halt_reason(spec_dir),
        injection_scan=_read_injection_scan(spec_dir),
        tenant_id=_read_tenant_id(spec_dir),
    )
    # Serial single-'main'-worker live sub-event (#45 P1). Parallel workers each
    # emit their live sub-event as they finish (agents/parallel_integration.py);
    # a serial build has no fan-out, so its single implicit 'main' worker has no
    # live event yet — emit it here, on completion. Guarded so it's restricted to
    # exactly the serial case (one 'main' worker) and never duplicates a parallel
    # worker's live event. Best-effort: never affects the terminal event below.
    try:
        workers = (usage or {}).get("workers") or []
        if len(workers) == 1 and str(workers[0].get("worker_id")) == "main":
            emit_worker_completion(
                spec_dir=spec_dir,
                task_id=task_id,
                spec_id=spec_id,
                project_id=project_id,
                worker=workers[0],
            )
    except Exception:  # noqa: BLE001 — must never break the terminal event
        logger.debug(
            "serial main-worker sub-event skipped (best-effort)", exc_info=True
        )
    delivered = notify_completion(event, spec_dir=spec_dir)
    event["_delivered"] = delivered
    return event


def emit_usage_snapshot(  # noqa: PLR0913 - event fields, all required by the envelope
    spec_dir: Path,
    *,
    task_id: str,
    project_id: str,
    spec_id: str,
    status: str,
    usage: dict | None = None,
) -> dict | None:
    """Emit a NON-terminal, usage-bearing event so the cockpit reflects RUNNING
    cost even when a task pauses for human review (or otherwise stops without a
    terminal ``done``/``failed``).

    Cost accrues continuously — a build that paused at review or never finished
    has already spent real tokens — so usage must reach CFactory independent of
    terminal completion (``emit_terminal_completion`` only fires on terminal
    states). This rides the SAME envelope + transport (``build_completion_event``
    + ``notify_completion``), carrying the current ``usage`` block with the
    non-terminal ``status``; CFactory records usage from any event that carries
    it. Best-effort: returns the event, or ``None`` when there is nothing to
    report (no usage yet) — never raises.
    """
    # ``usage`` supplied → a LIVE snapshot off the log stream, where the spec dir
    # holds no token_usage.json yet (#1249). Falling back to the file when it is
    # absent would silently emit nothing, which is the bug being fixed.
    if usage is None:
        usage = read_usage(spec_dir)
    if usage is None:
        return None
    try:
        event = build_completion_event(
            task_id=task_id,
            spec_id=spec_id,
            status=status,
            issue_number=read_issue_number(spec_dir),
            project_id=project_id,
            usage=usage,
            injection_scan=_read_injection_scan(spec_dir),
            tenant_id=_read_tenant_id(spec_dir),
        )
        notify_completion(event, spec_dir=spec_dir)
        return event
    except Exception:  # noqa: BLE001 — usage reporting must never break the caller
        logger.debug("usage snapshot emit failed (best-effort)", exc_info=True)
        return None


def _read_injection_scan(spec_dir: Path) -> dict | None:
    """The pre-coder untrusted-content scan verdict for this task, if any
    (#805 / Factory#273). Written by ``security.content_scan`` to
    ``injection_scan.json`` in the spec dir (synced from the worktree)."""
    try:
        data = json.loads((spec_dir / "injection_scan.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in ("pass", "flagged", "skipped"):
        return None
    scan: dict = {"verdict": verdict}
    if isinstance(data.get("mode"), str):
        scan["mode"] = data["mode"]
    if isinstance(data.get("matched"), list):
        scan["matched"] = data["matched"]
    return scan


def _read_halt_reason(spec_dir: Path) -> str | None:
    """The Act-loop guardrail's typed no-progress reason for this run, if any
    (#474). Written by ``agents.act_loop_hooks`` to ``guardrail_halt.json``."""
    try:
        data = json.loads((spec_dir / "guardrail_halt.json").read_text())
        reason = data.get("halt_reason")
        return reason if isinstance(reason, str) else None
    except (OSError, ValueError):
        return None
