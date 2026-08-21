"""
Per-Category Token Attribution (#262)
=====================================

Breaks a session's token usage down by **source category** so the web UI can
show *where* a build's context budget is going: user instructions vs. the
static system/CLAUDE.md prefix vs. tool outputs vs. thinking text vs.
team/coordination context.

Design
------
The Claude Agent SDK reports *real* aggregate usage per turn on
``ResultMessage.usage`` (``input_tokens``, ``output_tokens``,
``cache_read_input_tokens``, ``cache_creation_input_tokens``) and a real
``total_cost_usd``. Those numbers are authoritative for *totals* but the SDK
does **not** tell us how the input split across the prompt segments AIFactory
assembled (system prompt, subtask prompt, file context, graphiti/coordination
context, inbox messages). So this module:

  1. takes the **real** SDK input-token total for the turn, and
  2. apportions it across categories using an **estimated** per-segment split
     derived from the segment strings the agent loop already has in hand.

The estimator is a documented char-based heuristic (``CHARS_PER_TOKEN``) — no
heavy tokenizer dependency is added (the repo has no ``tiktoken``; the
``anthropic`` count-tokens API is a network round-trip and unsuitable for a
hot per-turn path). The heuristic is only used to compute *relative weights*;
the absolute totals always come from the SDK, so a mis-calibrated
chars/token constant changes the within-input split slightly but never the
reported total. ``output_tokens`` and any cache figures are attributed to
fixed categories (``thinking_and_output`` / cache is folded into the cached
system prefix) rather than estimated.

This module is deliberately SDK-independent (pure Python + stdlib) so it can
be unit-tested without the Claude SDK installed and imported from both the
backend agent loop and, indirectly, surfaced by the web-server.

On-disk contract (AIFactory-owned, shared with the web-server reader):

    <spec_dir>/token_usage.json   →  aggregated per-task breakdown (atomic write)
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from routing_policy import tier_for_model

# Documented heuristic: average English-prose / source-code tokenization is
# ~3.5–4 chars per token for Claude models. We use 4.0 as a stable, widely
# cited default. Only affects the *relative* split, never SDK-sourced totals.
CHARS_PER_TOKEN = 4.0

USAGE_FILE_NAME = "token_usage.json"

# Canonical category taxonomy. ``key`` is the stable machine id (used in the
# persisted file + API); ``label`` is a human string; ``color`` is a UI hint.
# Order is the canonical display order.
CATEGORY_LABELS: dict[str, str] = {
    "system_instructions": "System / CLAUDE.md / AGENTS.md",
    "user_messages": "User messages",
    "coordination_context": "Team / coordination context",
    "tool_outputs": "Tool outputs",
    "thinking_and_output": "Thinking + assistant output",
}
CATEGORY_COLORS: dict[str, str] = {
    "system_instructions": "#6366f1",
    "user_messages": "#22c55e",
    "coordination_context": "#06b6d4",
    "tool_outputs": "#f59e0b",
    "thinking_and_output": "#a855f7",
}

# Categories funded from the *fresh* (non-cached) input token budget; their
# split is estimated from prompt segments. ``system_instructions`` is funded
# separately from cached tokens (the static system/CLAUDE.md prefix is exactly
# what the prompt cache holds), so it is NOT in this list.
_FRESH_INPUT_CATEGORIES = (
    "user_messages",
    "coordination_context",
    "tool_outputs",
)


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a string via the char heuristic.

    Documented approximation (see ``CHARS_PER_TOKEN``). Used only to weight
    the per-category split of a turn's real input-token total.
    """
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


@dataclass
class PromptSegments:
    """The named pieces that make up a turn's *input*, as the agent loop
    assembles them. Tokens are attributed to categories from these strings.

    All fields are optional; callers pass whatever they have for the turn.
    ``tool_output_chars`` lets the caller report observed tool-result volume
    (which is part of the input on the *next* turn) without holding the full
    strings in memory.
    """

    system_prompt: str = ""  # CLAUDE.md + AGENTS.md + base instructions
    user_prompt: str = ""  # the subtask/user instruction prompt
    coordination_context: str = ""  # graphiti / inbox / team coordination blocks
    file_context: str = ""  # injected source-file context (counts as user)
    tool_output_chars: int = 0  # observed tool-result characters this turn

    def category_weights(self) -> dict[str, int]:
        """Estimated token weight per *fresh-input* category (pre-normalisation).

        ``system_instructions`` is intentionally excluded — it is funded from
        the turn's cached tokens, not estimated here.
        """
        return {
            # File context is part of what the user/task asked to work on.
            "user_messages": estimate_tokens(self.user_prompt)
            + estimate_tokens(self.file_context),
            "coordination_context": estimate_tokens(self.coordination_context),
            "tool_outputs": max(0, round(self.tool_output_chars / CHARS_PER_TOKEN)),
        }


@dataclass
class TurnUsage:
    """A normalised view of one turn's *real* SDK usage."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_input_tokens(self) -> int:
        """All input-side tokens billed this turn, including cache."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    @classmethod
    def from_sdk_usage(
        cls, usage: dict[str, Any] | None, cost_usd: float | None = None
    ) -> TurnUsage:
        """Build from a ``ResultMessage.usage`` dict (any missing keys -> 0)."""
        usage = usage or {}
        return cls(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cost_usd=float(cost_usd or 0.0),
        )


@dataclass
class CategoryBreakdown:
    """Per-category result for a turn or an aggregate."""

    tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self, key: str, max_tokens: int) -> dict[str, Any]:
        pct = (self.tokens / max_tokens * 100.0) if max_tokens > 0 else 0.0
        return {
            "key": key,
            "label": CATEGORY_LABELS.get(key, key),
            "color": CATEGORY_COLORS.get(key, "#94a3b8"),
            "tokens": self.tokens,
            "pctOfWindow": round(pct, 2),
            "costUsd": round(self.cost_usd, 6),
        }


@dataclass
class TurnAttribution:
    """A full per-category attribution for one turn."""

    categories: dict[str, CategoryBreakdown] = field(default_factory=dict)
    total_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def attribute_turn(segments: PromptSegments, usage: TurnUsage) -> TurnAttribution:
    """Attribute one turn's real SDK usage across the category taxonomy.

    - **Cached** input tokens (``cache_read`` + ``cache_creation``) are
      attributed to ``system_instructions`` — the static system/CLAUDE.md
      prefix is exactly what AIFactory caches (see ``core.client``). When the
      provider reports no cache (e.g. a local OpenAI-compatible endpoint), the
      system prompt's estimated token weight is folded into the fresh-input
      split instead, so the category is never silently dropped.
    - The **fresh** (non-cached) ``input_tokens`` are split across
      ``user_messages`` / ``coordination_context`` / ``tool_outputs`` in
      proportion to estimated per-segment weights.
    - ``output_tokens`` is attributed wholly to ``thinking_and_output``.
    - ``cost_usd`` is apportioned by each category's share of *all* tokens
      (input + output) so per-category cost sums to the SDK's real total.

    Totals always reconcile exactly: the per-category integers sum to the
    real ``total_input_tokens`` regardless of the estimation.
    """
    cats: dict[str, CategoryBreakdown] = {
        k: CategoryBreakdown() for k in CATEGORY_LABELS
    }

    cached_tokens = usage.cache_read_tokens + usage.cache_creation_tokens
    fresh_tokens = usage.input_tokens

    weights = dict(segments.category_weights())
    if cached_tokens > 0:
        # System prefix is accounted for by the cache figure.
        cats["system_instructions"].tokens = cached_tokens
        fresh_keys = list(_FRESH_INPUT_CATEGORIES)
    else:
        # No cache reported — estimate the system prefix from its segment and
        # carve it out of the fresh input alongside the others.
        weights["system_instructions"] = estimate_tokens(segments.system_prompt)
        fresh_keys = ["system_instructions", *_FRESH_INPUT_CATEGORIES]

    weight_sum = sum(weights[k] for k in fresh_keys)
    if fresh_tokens > 0:
        if weight_sum <= 0:
            # No segment info — drop the whole fresh input on user_messages
            # rather than fabricate a split. Totals still reconcile.
            cats["user_messages"].tokens += fresh_tokens
        else:
            # Largest-remainder apportionment: per-category integers sum to
            # exactly ``fresh_tokens`` (no rounding drift).
            allocated = 0
            remainders: list[tuple[float, str]] = []
            for key in fresh_keys:
                exact = fresh_tokens * weights[key] / weight_sum
                base = int(exact)
                cats[key].tokens += base
                allocated += base
                remainders.append((exact - base, key))
            leftover = fresh_tokens - allocated
            for _, key in sorted(remainders, reverse=True)[: max(0, leftover)]:
                cats[key].tokens += 1

    cats["thinking_and_output"].tokens = usage.output_tokens

    real_input = usage.total_input_tokens

    # Apportion cost by total-token share.
    total_tokens = real_input + usage.output_tokens
    if total_tokens > 0 and usage.cost_usd > 0:
        for cat in cats.values():
            cat.cost_usd = usage.cost_usd * cat.tokens / total_tokens

    return TurnAttribution(
        categories=cats,
        total_input_tokens=real_input,
        output_tokens=usage.output_tokens,
        cost_usd=usage.cost_usd,
    )


def context_window_for_model(model: str | None) -> int:
    """Best-effort context-window size (tokens) for %-of-window math.

    Conservative defaults; unknown models fall back to 200k (Claude's common
    window). Callers may override by passing an explicit ``max_tokens``.
    """
    if not model:
        return 200_000
    m = model.lower()
    if "[1m]" in m or "-1m" in m or "1m" in m.split("-")[-1:]:
        return 1_000_000
    # Sonnet / Opus / Haiku 4.x and 3.x all expose a 200k window by default.
    return 200_000


def _empty_aggregate() -> dict[str, Any]:
    return {
        "version": 1,
        "turns": 0,
        "totalInputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "totalCostUsd": 0.0,
        "categories": {k: {"tokens": 0, "costUsd": 0.0} for k in CATEGORY_LABELS},
        "model": None,
        "maxTokens": 200_000,
        # Control-plane model fallbacks seen during this task (#1374). An empty
        # list means "no fallback happened", which a missing key cannot say.
        "fallbacks": [],
        "updatedAt": None,
        # Per-worker breakdown (#45 P1) — additive sibling of the scalar
        # aggregate above. Keyed by worker_id (a serial build = one implicit
        # worker, "main"). The scalar fields are KEPT verbatim for back-compat;
        # this map is the per-worker / per-provider drill-down.
        "workers": {},
    }


# Default worker id for the serial / single-implicit-worker path. A serial
# build has no subtask fan-out, so its whole spend is attributed to one
# implicit worker named "main".
DEFAULT_WORKER_ID = "main"

# Companion of ``phase_config.FALLBACK_MODEL_ENV``: the model a control-plane
# fallback DISPLACED (#1374). Defined here, not imported, because this module is
# deliberately stdlib-only and phase_config is imported from it lazily.
FALLBACK_FROM_ENV = "AIFACTORY_FALLBACK_FROM"


def _read_aggregate(usage_path: Path) -> dict[str, Any]:
    if not usage_path.exists():
        return _empty_aggregate()
    # Corrupt/missing usage file: fall back to a fresh cost-tracking
    # aggregate rather than crash the build over telemetry.
    with contextlib.suppress(json.JSONDecodeError, OSError):
        data = json.loads(usage_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "categories" in data:
            # Backfill any newly added categories.
            for k in CATEGORY_LABELS:
                data["categories"].setdefault(k, {"tokens": 0, "costUsd": 0.0})
            # Backfill the per-worker map for files written before #45 P1.
            data.setdefault("workers", {})
            # ... and the fallback list for files written before #1374.
            data.setdefault("fallbacks", [])
            return data
    return _empty_aggregate()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """tmp file in the same dir + ``os.replace`` (house style: see
    ``web-server/.../task_control.py`` and ``agents/inbox.py``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def usage_file_path(spec_dir: Path) -> Path:
    return Path(spec_dir) / USAGE_FILE_NAME


def _fold_worker(
    agg: dict[str, Any],
    *,
    worker_id: str,
    phase: str | None,
    subtask_id: str | None,
    provider: str | None,
    model: str | None,
    routing_metadata: dict[str, Any] | None = None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    duration_ms: int | None,
) -> None:
    """Fold one turn's usage into the per-worker record (in-place, additive).

    A worker = one subtask (a serial build = the single implicit ``"main"``
    worker). The scalar aggregate the rest of this module maintains is left
    untouched; this only updates ``agg["workers"][worker_id]``. Providers with
    no cost (local / Ollama) record ``cost_usd: 0`` but still accrue tokens +
    duration. (LiteLLM cost-mapping is a deliberate follow-up, not this PR.)
    """
    workers = agg.setdefault("workers", {})
    rec = workers.get(worker_id)
    if rec is None:
        rec = {
            "worker_id": worker_id,
            "phase": phase,
            "subtask_id": subtask_id,
            "provider": provider,
            "model": model,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "duration_ms": 0,
        }
        workers[worker_id] = rec
    # Last-writer-wins for the descriptive fields (a worker keeps one
    # provider/model across its turns; later turns can fill blanks).
    rec["phase"] = phase if phase is not None else rec.get("phase")
    rec["subtask_id"] = subtask_id if subtask_id is not None else rec.get("subtask_id")
    rec["provider"] = provider if provider is not None else rec.get("provider")
    rec["model"] = model if model is not None else rec.get("model")
    # RFC-0014 (#803): stamp the routing tier the active policy (env) OR the
    # contract routing block attributes to this worker's model, so the
    # completion envelope carries model + tier. No policy and no contract routing
    # block -> no key -> envelope unchanged.
    tier = tier_for_model(rec.get("model"), routing_metadata)
    if tier is not None:
        rec["routing_tier"] = tier
    rec["input_tokens"] = int(rec.get("input_tokens", 0)) + int(input_tokens)
    rec["output_tokens"] = int(rec.get("output_tokens", 0)) + int(output_tokens)
    rec["total_tokens"] = int(rec["input_tokens"]) + int(rec["output_tokens"])
    rec["cost_usd"] = round(float(rec.get("cost_usd", 0.0)) + float(cost_usd or 0.0), 6)
    if duration_ms is not None:
        rec["duration_ms"] = int(rec.get("duration_ms", 0)) + int(duration_ms)


def _stamp_fallback(agg: dict[str, Any], worker_id: str) -> None:
    """Record a control-plane model fallback in the artefact (#1374).

    ``phase_config`` makes the fallback model the one that actually resolves, so
    the fold above already names the model that RAN. That alone is not enough:
    corrected accounting reads exactly like a clean run of whatever ran last,
    which is how a benchmark leg that swapped models mid-run got published as a
    result for a single model. So also carry the DISPLACED model — per worker
    (``fallbackFrom``) and once at the top level (``fallbacks``) — so a reader
    can refuse to publish the leg.
    """
    displaced = os.environ.get(FALLBACK_FROM_ENV, "").strip()
    if not displaced:
        return
    rec = agg.get("workers", {}).get(worker_id)
    if rec is not None:
        rec["fallbackFrom"] = displaced
    entry = {"from": displaced, "to": (rec or {}).get("model"), "workerId": worker_id}
    fallbacks = agg.setdefault("fallbacks", [])
    if entry not in fallbacks:
        fallbacks.append(entry)


def record_turn(
    spec_dir: Path,
    segments: PromptSegments,
    usage: TurnUsage,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    worker_id: str | None = None,
    phase: str | None = None,
    subtask_id: str | None = None,
    provider: str | None = None,
    duration_ms: int,
) -> dict[str, Any]:
    """Attribute one turn and fold it into the per-task aggregate (atomic).

    Returns the rendered breakdown dict (same shape the API serves). Never
    raises on IO problems — token accounting must not break a build; on a
    read/write error it returns the freshly computed single-turn view.

    Per-worker breakdown (#45 P1, additive): the turn is ALSO folded into a
    ``workers`` map keyed by ``worker_id`` (the subtask id; a serial build is
    one implicit worker, ``"main"``). Every existing scalar aggregate field is
    written exactly as before — the workers map rides alongside, never replaces.
    ``provider``/``subtask_id``/``phase`` describe the worker; when omitted they
    default sensibly (``worker_id`` → ``"main"``).

    ``duration_ms`` is REQUIRED, and deliberately has no default (#1100). It
    used to default to ``None``, which the fold then skipped — so the serial
    coder, which simply never passed it, produced ``duration_ms: 0`` for every
    worker for the life of the run. 0 read as a plausible measurement, not as
    "unmeasured", and the swarm comparison it feeds (Factory#345 wall-clock)
    silently had no denominator. A required kwarg makes the omission a
    TypeError at the call site instead of a zero in an artifact.
    """
    attribution = attribute_turn(segments, usage)
    window = max_tokens or context_window_for_model(model)
    wid = worker_id or DEFAULT_WORKER_ID
    sub_id = (
        subtask_id
        if subtask_id is not None
        else (worker_id if worker_id and worker_id != DEFAULT_WORKER_ID else None)
    )
    # Routing metadata for the per-worker tier stamp (#803): when a contract
    # routing block is present it lets tier_for_model reverse-map the tier even
    # without a local env policy. Lazy import avoids a phase_config <->
    # token_attribution cycle; best-effort — the stamp must never break
    # accounting.
    routing_metadata: dict[str, Any] | None = None
    try:
        from phase_config import load_task_metadata  # noqa: PLC0415

        loaded = load_task_metadata(spec_dir)
        routing_metadata = loaded if isinstance(loaded, dict) else None
    except Exception:  # noqa: BLE001 - stamping is best-effort
        routing_metadata = None

    try:
        agg = _read_aggregate(usage_file_path(spec_dir))
        agg["turns"] = int(agg.get("turns", 0)) + 1
        agg["totalInputTokens"] = (
            int(agg.get("totalInputTokens", 0)) + attribution.total_input_tokens
        )
        agg["outputTokens"] = (
            int(agg.get("outputTokens", 0)) + attribution.output_tokens
        )
        # Persist the combined input+output total so raw-file consumers (the
        # #277 token-usage panel and any reader that doesn't go through
        # ``render_breakdown``) see a real value instead of a missing key (#288).
        agg["totalTokens"] = int(agg.get("totalInputTokens", 0)) + int(
            agg.get("outputTokens", 0)
        )
        agg["totalCostUsd"] = round(
            float(agg.get("totalCostUsd", 0.0)) + attribution.cost_usd, 6
        )
        for key, cat in attribution.categories.items():
            slot = agg["categories"].setdefault(key, {"tokens": 0, "costUsd": 0.0})
            slot["tokens"] = int(slot.get("tokens", 0)) + cat.tokens
            slot["costUsd"] = round(float(slot.get("costUsd", 0.0)) + cat.cost_usd, 6)
        agg["model"] = model or agg.get("model")
        agg["maxTokens"] = window
        # Per-worker fold (#45 P1, additive) — alongside the scalar aggregate.
        _fold_worker(
            agg,
            worker_id=wid,
            phase=phase,
            subtask_id=sub_id,
            provider=provider,
            model=model,
            routing_metadata=routing_metadata,
            input_tokens=attribution.total_input_tokens,
            output_tokens=attribution.output_tokens,
            cost_usd=attribution.cost_usd,
            duration_ms=duration_ms,
        )
        _stamp_fallback(agg, wid)
        agg["updatedAt"] = datetime.now(UTC).isoformat()
        _atomic_write_json(usage_file_path(spec_dir), agg)
        # #1249: the file above is the durable record, but under the kubejob
        # backend it lives in the Job's ephemeral /work and reaches the control
        # plane only when it is pushed back at the END of the build — so the
        # cockpit showed no cost accruing at all while a build ran. stdout IS
        # followed live, so mirror the aggregate onto it (throttled at the
        # source). Best-effort: never let cost reporting break attribution.
        # suppress(Exception): cost reporting must never be able to break
        # attribution, and there is nothing useful to do with a failure here.
        with contextlib.suppress(Exception):
            from core.phase_event import emit_usage  # noqa: PLC0415

            emit_usage(agg)
        return render_breakdown(agg)
    except OSError:
        # Fall back to a single-turn render so callers still get data.
        single = _empty_aggregate()
        single["turns"] = 1
        single["totalInputTokens"] = attribution.total_input_tokens
        single["outputTokens"] = attribution.output_tokens
        single["totalTokens"] = (
            attribution.total_input_tokens + attribution.output_tokens
        )
        single["totalCostUsd"] = round(attribution.cost_usd, 6)
        single["model"] = model
        single["maxTokens"] = window
        for key, cat in attribution.categories.items():
            single["categories"][key] = {
                "tokens": cat.tokens,
                "costUsd": round(cat.cost_usd, 6),
            }
        _fold_worker(
            single,
            worker_id=wid,
            phase=phase,
            subtask_id=sub_id,
            provider=provider,
            model=model,
            routing_metadata=routing_metadata,
            input_tokens=attribution.total_input_tokens,
            output_tokens=attribution.output_tokens,
            cost_usd=attribution.cost_usd,
            duration_ms=duration_ms,
        )
        return render_breakdown(single)


def render_breakdown(agg: dict[str, Any]) -> dict[str, Any]:
    """Turn a stored aggregate into the API/UI response shape.

    %-of-window is computed against ``maxTokens`` using the *current* total
    input tokens (the live context pressure), not cumulative across turns —
    cumulative input would exceed the window and is not meaningful as a %.
    Cumulative figures are still exposed as raw totals.
    """
    max_tokens = int(agg.get("maxTokens", 200_000)) or 200_000
    total_input = int(agg.get("totalInputTokens", 0))
    categories = []
    for key in CATEGORY_LABELS:
        slot = agg.get("categories", {}).get(key, {"tokens": 0, "costUsd": 0.0})
        cb = CategoryBreakdown(
            tokens=int(slot.get("tokens", 0)),
            cost_usd=float(slot.get("costUsd", 0.0)),
        )
        categories.append(cb.as_dict(key, max_tokens))

    total_tokens = total_input + int(agg.get("outputTokens", 0))
    return {
        "version": agg.get("version", 1),
        "turns": int(agg.get("turns", 0)),
        "model": agg.get("model"),
        "maxTokens": max_tokens,
        "totalInputTokens": total_input,
        "outputTokens": int(agg.get("outputTokens", 0)),
        "totalTokens": total_tokens,
        "totalCostUsd": round(float(agg.get("totalCostUsd", 0.0)), 6),
        "pctOfWindow": round(
            (total_input / max_tokens * 100.0) if max_tokens else 0.0, 2
        ),
        "categories": categories,
        "updatedAt": agg.get("updatedAt"),
    }


def read_breakdown(spec_dir: Path) -> dict[str, Any]:
    """Read the persisted per-task breakdown (empty shape if absent)."""
    return render_breakdown(_read_aggregate(usage_file_path(spec_dir)))
