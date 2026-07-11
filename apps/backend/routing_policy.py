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

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "AIFACTORY_ROUTING_POLICY"


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


def _str_map(policy: dict[str, Any], key: str) -> dict[str, str]:
    raw = policy.get(key)
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, str) and v}


def policy_route(stage: str) -> tuple[str, str] | None:
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
    model = _str_map(policy, "tiers").get(tier)
    if model is None:
        logger.warning(
            "routing policy maps stage %r to unknown tier %r; using default",
            stage,
            tier,
        )
        return None
    return model, tier


def tier_for_model(model: str | None) -> str | None:
    """Reverse lookup: the policy tier whose model resolves to ``model``.

    Used to stamp ``routing_tier`` next to the actually-used model in the
    per-worker usage records (completion envelope v1.3). Returns ``None`` when
    no policy is active or the model matches no tier — the stamp is then
    simply omitted, keeping the envelope byte-identical to today.
    """
    if not model:
        return None
    policy = load_policy()
    if policy is None:
        return None
    # Lazy import: phase_config imports this module for resolution, so the
    # shorthand resolver must be imported at call time to avoid a cycle.
    from phase_config import resolve_model_id  # noqa: PLC0415

    target = resolve_model_id(model)
    for tier, tier_model in _str_map(policy, "tiers").items():
        if tier_model == model or resolve_model_id(tier_model) == target:
            return tier
    return None
