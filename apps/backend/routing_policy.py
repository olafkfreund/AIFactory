"""RFC-0014 v1 per-stage model routing policy (#803).

Opt-in via the ``AIFACTORY_ROUTING_POLICY`` environment variable, which holds
either the policy JSON itself or a path to a JSON file (Helm configmap mount).
Shape::

    {
      "tiers":  {"small": "haiku", "mid": "sonnet", "frontier": "opus"},
      "stages": {"planning": "frontier", "coding": "mid", "qa": "small",
                 "qa_fixer": "small", "spec": "mid"}
    }

Tier values are model strings in any form ``phase_config.resolve_model_id``
accepts: a shorthand (``sonnet``), a full id (``claude-opus-4-8``), or a
provider-prefixed local/subscription form (``ollama:qwen3:14b``).

Precedence (wired in ``phase_config.get_phase_model``)::

    contract routing.pinned_model > per-task override > policy tier > default

Fail-closed contract: an absent/unparseable policy, an unmapped stage, or a
stage mapped to an unknown tier all yield ``None`` — the caller falls through
to today's default behaviour, so a broken policy can never change routing.
Stdlib-only (imported by the pure token-attribution path).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "AIFACTORY_ROUTING_POLICY"

# Default tier -> model (RFC-0014 §2b catalog mapping: cheap->small, balanced->
# mid, frontier->frontier). Used to map a CONTRACT-supplied stage tier when the
# local AIFACTORY_ROUTING_POLICY provides no `tiers` map of its own. Shorthands;
# phase_config.resolve_model_id expands them.
DEFAULT_TIERS = {"small": "haiku", "mid": "sonnet", "frontier": "opus"}

# Model tier rank, weakest -> strongest. A contract tier is never floored BELOW
# the RFC-0011 difficulty tier's requirement (a `hard` task keeps a frontier
# coder even if the policy asked for `mid`); the floor may only RAISE it.
_TIER_RANK = {"small": 0, "mid": 1, "frontier": 2}
_RANK_TIER = {v: k for k, v in _TIER_RANK.items()}

# RFC-0011 difficulty tier -> capability floor (model tier). Unknown/missing
# tiers impose no floor (None) so an unrecognized signal cannot cheapen OR
# inflate a task beyond what the routing policy asked for.
_RFC0011_FLOOR = {"low": "small", "medium": "mid", "hard": "frontier"}

# Factory#1959 slice 5: resolve a route tier to a model from a catalog file read
# at runtime (a mounted ConfigMap) instead of the baked DEFAULT_TIERS. off|shadow|on,
# default off; an unknown value counts as off. Unset/unreadable catalog = inert.
ROUTE_RESOLUTION_ENV = "AIFACTORY_ROUTE_RESOLUTION"
CATALOG_PATH_ENV = "AIFACTORY_MODEL_CATALOG_PATH"
_ROUTE_MODES = ("off", "shadow", "on")
# Contract tier -> RFC-0014 catalog capability class (§2b, see DEFAULT_TIERS).
_TIER_CLASS = {"small": "cheap", "mid": "balanced", "frontier": "frontier"}
# AIFactory stage -> catalog role, where the names differ.
_STAGE_ROLE = {"spec": "planning", "qa_fixer": "qa"}


def load_policy() -> dict[str, Any] | None:
    """Parse the routing policy from ``AIFACTORY_ROUTING_POLICY``.

    The variable carries either the JSON document itself or a filesystem path
    to it. Returns ``None`` (fail-closed to default routing) when the variable
    is unset/empty, the file is unreadable, or the content is not a JSON object.
    """
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return None
    text = raw
    if not raw.startswith("{"):
        try:
            text = Path(raw).read_text(encoding="utf-8")
        except OSError:
            logger.warning("routing policy file %r unreadable; policy ignored", raw)
            return None
    try:
        data = json.loads(text)
    except ValueError:
        logger.warning("routing policy is not valid JSON; policy ignored")
        return None
    if not isinstance(data, dict):
        logger.warning("routing policy is not a JSON object; policy ignored")
        return None
    return data


_warned_modes: set[str] = set()


def _route_mode() -> str:
    raw = os.environ.get(ROUTE_RESOLUTION_ENV, "").strip().lower() or "off"
    if raw not in _ROUTE_MODES:
        if raw not in _warned_modes:  # once per process per value, not per call
            _warned_modes.add(raw)
            logger.warning(
                "%s=%r is not off|shadow|on; treating as off", ROUTE_RESOLUTION_ENV, raw
            )
        return "off"
    return raw


def _catalog_picks(tiers: list[str], stage: str) -> dict[str, str | None]:
    """The catalog's model per route tier at ``stage``; empty when inert.

    Loads the catalog once for all ``tiers``. Same pick rule as PFactory's
    cost_router: the hub router over Claude-only, available entries
    (#779/#826), at each tier's capability class. Any failure (unreadable file,
    malformed entry) is inert: the caller keeps its default model.
    """
    path = os.environ.get(CATALOG_PATH_ENV, "").strip()
    if not path:
        return {}
    # Lazy, only when resolution is enabled: keeps this module stdlib-only for
    # the token-attribution path. import_module because mypy, run from the repo
    # root by the CQ ratchet, cannot resolve backend-local packages.
    crc = importlib.import_module("core.cost_router_core")
    role = _STAGE_ROLE.get(stage, stage)
    # shortcut: re-read per resolution (hot reload, no cache); add an mtime
    # cache if it shows in profiles.
    try:
        candidates = {
            mid: e
            for mid, e in crc.load_catalog(path).items()
            if e.get("provider") == "claude" and e.get("available") is not False
        }
        # Any positive token estimate: only the price ratio orders metered models.
        return {
            t: crc.cheapest_capable_model(
                role, _TIER_CLASS[t], None, (1, 1), candidates
            )
            for t in tiers
            if t in _TIER_CLASS
        }
    except Exception as exc:  # noqa: BLE001 - a bad catalog must never break routing
        # Exception type only: never log catalog contents.
        logger.warning(
            "model catalog %r unusable (%s); route resolution inert",
            path,
            type(exc).__name__,
        )
        return {}


def _tier_model(tier: str, stage: str) -> str | None:
    """Model for a route ``tier`` when no local policy ``tiers`` map names it."""
    default = DEFAULT_TIERS.get(tier)
    mode = _route_mode()
    if mode == "off":
        return default
    picked = _catalog_picks([tier], stage).get(tier)
    if mode == "shadow":
        logger.info(
            "route resolution (shadow): stage %s tier %s -> catalog %s (using default %s)",
            stage,
            tier,
            picked,
            default,
        )
        return default
    return picked or default


def _str_map(policy: dict[str, Any], key: str) -> dict[str, str]:
    raw = policy.get(key)
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, str) and v}


def policy_route(
    stage: str, difficulty_tier: str | None = None
) -> tuple[str, str] | None:
    """Resolve ``stage`` through the active policy to ``(model, tier)``.

    ``model`` is the raw policy value (shorthand or full id — the caller
    resolves it). Returns ``None`` when there is no policy, the stage is not
    mapped, or the stage's tier has no model (unknown tier fails closed to the
    caller's default).
    """
    policy = load_policy()
    if policy is None:
        return None
    tier = _str_map(policy, "stages").get(stage)
    if tier is None:
        return None
    # RFC-0011 capability floor (#825 follow-up): never route a stage BELOW the
    # difficulty tier's floor (a hard task keeps a frontier coder even if the
    # policy asked for something cheaper). Only raises; a policy already at/above
    # the floor is unchanged, and a custom (non-small/mid/frontier) tier name is
    # left as-is since it has no rank to floor against.
    if tier in _TIER_RANK:
        tier = _floor_tier(tier, difficulty_tier)
    model = _str_map(policy, "tiers").get(tier) or _tier_model(tier, stage)
    if model is None:
        logger.warning(
            "routing policy maps stage %r to unknown tier %r; using default",
            stage,
            tier,
        )
        return None
    return model, tier


def _floor_tier(tier: str, difficulty_tier: str | None) -> str:
    """Raise *tier* to the RFC-0011 difficulty capability floor; never lower it."""
    floor = _RFC0011_FLOOR.get(str(difficulty_tier or "").lower())
    if floor is None:
        return tier
    return _RANK_TIER[max(_TIER_RANK[tier], _TIER_RANK[floor])]


def contract_route(
    stage: str, metadata: dict[str, Any] | None
) -> tuple[str, str] | None:
    """Resolve ``stage`` through the contract routing block in task metadata.

    Consumes the RFC-0014 ``routing.requested`` / ``routing.overrides`` tiers
    that PFactory writes into the signed contract (carried to task_metadata as
    ``routingRequested`` / ``routingOverrides``). Precedence within the contract:
    ``overrides[stage]`` > ``requested[stage]``. The tier is floored by the
    RFC-0011 difficulty tier (``difficultyTier``), then mapped to a model via the
    local policy's ``tiers`` map, else :data:`DEFAULT_TIERS`.

    Returns ``(model, tier)`` or ``None`` when the contract routes no tier for
    this stage — the caller then falls through to the env policy / default, so a
    contract without a routing block is byte-identical to today.
    """
    if not isinstance(metadata, dict):
        return None
    overrides = metadata.get("routingOverrides")
    requested = metadata.get("routingRequested")
    tier = None
    if isinstance(overrides, dict) and overrides.get(stage) in _TIER_RANK:
        tier = overrides[stage]
    elif isinstance(requested, dict) and requested.get(stage) in _TIER_RANK:
        tier = requested[stage]
    if tier is None:
        return None
    tier = _floor_tier(tier, metadata.get("difficultyTier"))
    policy = load_policy() or {}
    policy_tiers = _str_map(policy, "tiers")
    model = policy_tiers.get(tier) if policy_tiers else _tier_model(tier, stage)
    if model is None:
        return None
    return model, tier


def tier_for_model(
    model: str | None,
    metadata: dict[str, Any] | None = None,
    stage: str | None = None,
) -> str | None:
    """Reverse lookup: the tier whose model resolves to ``model``.

    Used to stamp ``routing_tier`` next to the actually-used model in the
    per-worker usage records (completion envelope v1.3). Consults the env
    policy's ``tiers`` map; when a CONTRACT routing block is active for this
    build (``metadata`` carries ``routingRequested``/``routingOverrides``) it
    also reverse-maps against :data:`DEFAULT_TIERS` so contract-driven runs stamp
    the tier even without a local env policy. Returns ``None`` — no stamp — when
    neither a policy nor a contract routing block is present, keeping the
    envelope byte-identical to today.
    """
    if not model:
        return None
    policy = load_policy()
    tiers = _str_map(policy, "tiers") if policy else {}
    contract_active = isinstance(metadata, dict) and bool(
        metadata.get("routingRequested") or metadata.get("routingOverrides")
    )
    if not tiers and not contract_active:
        return None
    if not tiers:
        tiers = DEFAULT_TIERS
        if _route_mode() == "on":
            # Reverse-map the catalog pick too, so the stamp matches what ran.
            # Strongest first: a floor pick can serve several tiers, and the
            # highest tier it serves is its own class.
            # ``stage`` gives the real role; unknown (None) reads as coding.
            order = list(reversed(DEFAULT_TIERS))
            picks = _catalog_picks(order, stage or "coding")
            tiers = {t: picks.get(t) or DEFAULT_TIERS[t] for t in order}
    # Lazy import: phase_config imports this module for resolution, so the
    # shorthand resolver must be imported at call time to avoid a cycle.
    from phase_config import resolve_model_id  # noqa: PLC0415

    target = resolve_model_id(model)
    for tier, tier_model in tiers.items():
        if tier_model == model or resolve_model_id(tier_model) == target:
            return tier
    return None
