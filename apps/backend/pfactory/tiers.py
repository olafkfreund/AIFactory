"""Difficulty-tier classifier for RFC-0011 label-driven intake.

Pure, dependency-free. Given the labels on an issue/work-item, decide the
difficulty *tier* that drives model / planning / human-gate / verification /
merge policy across the fleet (see RFC-0011 routing table).

Three tiers, highest-wins precedence::

    factory:hard  >  factory:medium  >  factory:low

A rewrite (``change_mode == "migration"``) always **forces hard**, regardless
of the labels present, because equivalence verification is mandatory for
rewrites (RFC-0010).

The classifier is tolerant of *scoped* label spellings — both ``factory:low``
and ``factory::low`` (GitLab scoped-label syntax) are recognised — so the same
canonical tier survives a round-trip through any tracker.

``classify_parallel`` reads a SEPARATE, orthogonal axis off the same labels:
whether the build may run its independent subtasks concurrently
(``factory:parallel`` / ``factory:serial`` / ``factory:workers=N``).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from enum import Enum

from providers._ollama_http import resolve_ollama_base_url

__all__ = [
    "PARALLEL_LABEL",
    "SERIAL_LABEL",
    "TIER_LABEL_PREFIX",
    "WORKERS_LABEL_PREFIX",
    "Tier",
    "classify_parallel",
    "classify_tier",
    "resolve_low_tier_model",
    "tier_for",
]

logger = logging.getLogger(__name__)

# Model the low tier falls back to when no local Ollama is reachable.
_HAIKU_FALLBACK = "haiku"
# Short timeout — the low tier must not block intake waiting on a dead Ollama.
_PROBE_TIMEOUT_S = 1.5

TIER_LABEL_PREFIX = "factory:"

# Parallel-execution opt-in / opt-out labels (a separate axis from the tier).
PARALLEL_LABEL = "factory:parallel"
SERIAL_LABEL = "factory:serial"
WORKERS_LABEL_PREFIX = "factory:workers="

# Migration / rewrite change-mode that forces the hard tier.
_MIGRATION = "migration"


class Tier(str, Enum):
    """The three RFC-0011 difficulty tiers (ordered low < medium < hard)."""

    LOW = "low"
    MEDIUM = "medium"
    HARD = "hard"


# Precedence rank — highest wins.
_RANK: dict[Tier, int] = {Tier.LOW: 1, Tier.MEDIUM: 2, Tier.HARD: 3}


def _normalize(labels: object) -> list[str]:
    """Coerce labels (plain strings or ``{"name": ...}`` dicts) to a clean list.

    Tolerant by design: ``None``, non-iterables, blank names, and unexpected
    shapes are dropped rather than raising — intake must never crash on a
    malformed label set.
    """
    if not isinstance(labels, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for lbl in labels:
        name = lbl.get("name", "") if isinstance(lbl, dict) else lbl
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name:
            out.append(name)
    return out


def _tier_from_label(name: str) -> Tier | None:
    """Map a single label name to a ``Tier`` if it is a tier label, else ``None``.

    Accepts both ``factory:low`` and the GitLab scoped form ``factory::low``.
    """
    low = name.lower()
    # Collapse a scoped-label double colon to the canonical single colon.
    low = low.replace("::", ":")
    if not low.startswith(TIER_LABEL_PREFIX):
        return None
    value = low[len(TIER_LABEL_PREFIX) :].strip()
    try:
        return Tier(value)
    except ValueError:
        return None


def classify_tier(labels: object) -> Tier | None:
    """Return the highest-precedence tier among ``labels``, or ``None``.

    ``None`` means no ``factory:<tier>`` label was present (the caller decides
    a default). Highest wins: ``hard`` beats ``medium`` beats ``low``.
    """
    best: Tier | None = None
    for name in _normalize(labels):
        tier = _tier_from_label(name)
        if tier is None:
            continue
        if best is None or _RANK[tier] > _RANK[best]:
            best = tier
    return best


def classify_parallel(labels: object) -> tuple[bool | None, int | None]:
    """Return ``(parallel, workers)`` requested by ``labels``.

    A SEPARATE axis from the difficulty tier: parallelism is opted into per
    issue, independent of low/medium/hard.

    * ``factory:parallel`` -> ``True``  (run a wave's independent subtasks
      concurrently, each in its own git worktree — see agents/parallel_runner)
    * ``factory:serial``   -> ``False`` (explicit opt-OUT; always wins, so a
      deployment default can be overridden per issue)
    * neither              -> ``None``  (the caller applies its default)
    * ``factory:workers=N`` -> ``N`` (positive ints only; TUNES the worker cap,
      it does NOT by itself enable parallelism — pair it with
      ``factory:parallel``)

    Scoped spellings (``factory::parallel``) are accepted, matching
    :func:`classify_tier`. Malformed input never raises.
    """
    names = [n.lower().replace("::", ":") for n in _normalize(labels)]

    workers: int | None = None
    for name in names:
        if not name.startswith(WORKERS_LABEL_PREFIX):
            continue
        raw = name[len(WORKERS_LABEL_PREFIX) :].strip()
        # isdigit() rejects "-2"/"1.5"/"" without a try-except; >0 rejects "0".
        if raw.isdigit() and int(raw) > 0:
            workers = int(raw)  # last valid label wins

    # Opt-out beats opt-in regardless of label order: serial is the safe answer.
    if SERIAL_LABEL in names:
        return False, workers
    if PARALLEL_LABEL in names:
        return True, workers
    return None, workers


def tier_for(classification: object, change_mode: str | None = None) -> Tier:
    """Resolve the effective tier for a classification, applying overrides.

    Reads the ``tier`` attribute off a ``Classification`` (or any object
    exposing one); falls back to ``Tier.MEDIUM`` when unset so work is never
    routed below a safe default. A ``change_mode == "migration"`` (rewrite)
    always forces ``Tier.HARD``.
    """
    tier = getattr(classification, "tier", None)
    if not isinstance(tier, Tier):
        tier = Tier.MEDIUM
    if isinstance(change_mode, str) and change_mode.strip().lower() == _MIGRATION:
        return Tier.HARD
    return tier


def _probe_ollama_models(base_url: str) -> list[str]:
    """Return the list of model names from ``GET {base_url}/api/tags``.

    Returns an empty list on any error (unreachable, timeout, malformed JSON).
    Never raises — the low tier must degrade to haiku, not crash intake.
    """
    url = f"{base_url}/api/tags"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — defensive: any failure => fallback
        logger.debug("Ollama probe failed at %s: %s", url, exc)
        return []
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for m in models:
        name = m.get("name") if isinstance(m, dict) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def resolve_low_tier_model() -> str:
    """Pick the model id for the low tier: local Ollama if reachable, else haiku.

    Probes the Ollama ``/api/tags`` endpoint (short timeout) and, if at least
    one model is available, returns ``ollama:<first model>`` — which
    ``phase_config.infer_provider_from_model`` already routes to the Ollama
    provider. Otherwise returns ``"haiku"``. Defensive: never raises.
    """
    try:
        models = _probe_ollama_models(resolve_ollama_base_url())
    except Exception as exc:  # noqa: BLE001 — belt-and-braces, never raise
        logger.debug("resolve_low_tier_model fell back to haiku: %s", exc)
        return _HAIKU_FALLBACK
    if models:
        return f"ollama:{models[0]}"
    return _HAIKU_FALLBACK
