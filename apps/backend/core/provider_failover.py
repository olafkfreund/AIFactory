"""Provider bounded-retry + failover decisions (RFC-0008 §3.2 item c, #611).

The 2026-06-18 taskboard demo escalated to human review after a hung gemini CLI
timed out 300s x3 on the SAME provider — the system had no notion of failing
over to a different provider, and no overall time budget. This module is the
pure decision core for that:

- ``failover_chain(primary)`` — the ordered providers to try, primary first.
- ``next_provider(current, used)`` — the next un-tried provider, or ``None``.
- ``should_failover(error_text)`` — whether an error is worth switching provider
  for. A different provider can't fix a bad credential or a typo'd model name,
  so those short-circuit to escalation instead of burning the whole chain.
- ``DeadlineBudget`` — a monotonic overall deadline so failover retries can't
  run unbounded.

It holds NO I/O and no loop state, so the QA/build loops can consult it without
coupling to provider internals (which is where classification would be fragile).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable

from core import runtime_gating

# Default order mirrors providers.factory._TOOL_FALLBACK_ORDER (tool-capable,
# generally-available providers). Overridable via env for operators who run a
# different mix.
_DEFAULT_CHAIN: tuple[str, ...] = ("claude", "codex", "antigravity")

# Overall failover budget (seconds) across ALL providers for one phase. Default
# 900s ~= the old 3x300s wasted on a single hung provider, but now spent trying
# DIFFERENT providers instead of the same one.
_DEFAULT_DEADLINE_S = 900

# Substrings that mean "switching provider won't help — escalate now". These are
# config/credential/quota faults, not infra timeouts. Matched case-insensitively
# against the error text the loop already has.
_NO_FAILOVER_MARKERS: tuple[str, ...] = (
    "401",
    "403",
    "unauthorized",
    "authentication",
    "invalid api key",
    "invalid_api_key",
    "expired",
    "credential",
    "permission denied",
    "model not found",
    "model_not_found",
    "no such model",
    "unknown model",
    "insufficient_quota",
    "quota exceeded",
    "billing",
)


def configured_chain(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Resolve the failover order from ``AIFACTORY_PROVIDER_FAILOVER`` (a
    comma-separated provider list) or fall back to the default chain."""
    source = os.environ if env is None else env
    raw = (source.get("AIFACTORY_PROVIDER_FAILOVER") or "").strip()
    if not raw:
        return _DEFAULT_CHAIN
    parsed = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parsed or _DEFAULT_CHAIN


def failover_chain(primary: str, env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Ordered providers to try for a phase: ``primary`` first, then the
    configured chain, de-duplicated and lower-cased."""
    out: list[str] = []
    for p in (primary, *configured_chain(env)):
        name = (p or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def enabled_failover_chain(
    primary: str, env: dict[str, str] | None = None
) -> tuple[str, ...]:
    """The failover chain filtered to RFC-0014 operator-enabled runtimes.

    Failover must never reach for a runtime the operator has not enabled (that
    would spend on a gated runtime behind the operator's back). ``claude`` is
    always enabled, so the chain can never become empty; the primary is kept even
    when not allowlisted (the caller already chose it deliberately and gating is
    enforced at selection time by ``providers.factory.get_runtime_provider``).
    """
    allow = runtime_gating.operator_allowlist(env)
    primary_name = (primary or "").strip().lower()
    out: list[str] = []
    for name in failover_chain(primary, env):
        if name == primary_name or name in allow:
            out.append(name)
    return tuple(out)


def next_provider(
    primary: str,
    used: Iterable[str],
    env: dict[str, str] | None = None,
    *,
    gated: bool = False,
) -> str | None:
    """Return the next provider in the chain not already in ``used``, or
    ``None`` when the chain is exhausted (caller should escalate).

    When ``gated`` is True (RFC-0014 §6), the chain is filtered to operator-enabled
    runtimes so failover never reaches for a runtime the operator has not enabled.
    Default False preserves the pre-RFC-0014 behaviour for existing callers.
    """
    used_set = {(u or "").strip().lower() for u in used}
    chain = (
        enabled_failover_chain(primary, env) if gated else failover_chain(primary, env)
    )
    for candidate in chain:
        if candidate not in used_set:
            return candidate
    return None


def should_failover(error_text: str | None) -> bool:
    """True when an error is worth trying a different provider for.

    Returns False for credential / model-name / quota faults — a different
    provider can't fix those, and failing over would just burn the chain and
    hide the real cause. Defaults to True (infra/timeout/transport are the
    common case and ARE worth failing over).
    """
    if not error_text:
        return True
    lowered = error_text.lower()
    return not any(marker in lowered for marker in _NO_FAILOVER_MARKERS)


class DeadlineBudget:
    """A monotonic overall deadline for a phase's failover retries.

    Uses ``time.monotonic`` so it is immune to wall-clock changes.
    """

    def __init__(
        self, total_seconds: float | None = None, env: dict[str, str] | None = None
    ) -> None:
        if total_seconds is None:
            source = os.environ if env is None else env
            raw = (source.get("AIFACTORY_PROVIDER_DEADLINE_S") or "").strip()
            try:
                total_seconds = float(raw) if raw else _DEFAULT_DEADLINE_S
            except ValueError:
                total_seconds = _DEFAULT_DEADLINE_S
        self.total_seconds = float(total_seconds)
        self._start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0
