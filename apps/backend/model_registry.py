"""Approved-model registry (#323, #310).

Per-stage model config (``phase_config.DEFAULT_PHASE_MODELS`` / a Task Contract's
``phase_models`` / an ``AIFACTORY_ROUTING_POLICY`` tier) is freely editable, so a
model can be swapped into the coding or QA stage with no record of provenance and
no eval-gate. This module adds a declarative allowlist of approved models — each
with its provenance/version and the stages it may run — that ``phase_config``
consults as an assertion after it resolves a stage's model.

It is advisory by default: the check only covers first-party Claude models (the
swap-risk that matters); provider-prefixed local/third-party models
(``ollama:*``, ``openai:*``, ``codex``, ...) are out of scope and always pass,
since their catalogs cannot be enumerated here.

Enforcement mode — ``AIFACTORY_MODEL_REGISTRY_ENFORCE``:

* ``warn`` (default) — log a warning for an unregistered/mis-staged model, never
  block. Default builds use registered models, so this is silent in practice.
* ``deny`` — raise :class:`ModelNotApprovedError` (fail the stage). Opt-in.
* ``off`` — skip the check entirely (byte-identical to pre-registry behaviour).

Override the registry with ``AIFACTORY_MODEL_REGISTRY`` (inline JSON or a path to
a JSON file, same convention as ``routing_policy``); a broken value fails safe to
:data:`DEFAULT_REGISTRY`. See docs/docs/compliance/model-registry.md for the
eval-gate a model must pass before it is added here.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_REGISTRY = "AIFACTORY_MODEL_REGISTRY"
ENV_ENFORCE = "AIFACTORY_MODEL_REGISTRY_ENFORCE"

ALL_STAGES: frozenset[str] = frozenset({"spec", "planning", "coding", "qa", "qa_fixer"})


class ModelNotApprovedError(RuntimeError):
    """Raised (only in ``deny`` mode) when a stage resolves to a model that is
    not in the approved registry, or not approved for that stage."""


# Declarative allowlist keyed by full Claude model id. ``stages`` lists the
# execution phases the model is approved for; ``provenance``/``version`` document
# what it is and pin the exact release the eval-gate signed off on. Kept in sync
# with the shorthands in ``phase_config.MODEL_ID_MAP``.
DEFAULT_REGISTRY: dict[str, dict[str, Any]] = {
    "claude-opus-4-8": {
        "provenance": "Anthropic",
        "version": "opus-4.8",
        "stages": sorted(ALL_STAGES),
    },
    "claude-opus-4-7": {
        "provenance": "Anthropic",
        "version": "opus-4.7",
        "stages": sorted(ALL_STAGES),
    },
    "claude-opus-4-6": {
        "provenance": "Anthropic",
        "version": "opus-4.6 (1M beta)",
        "stages": sorted(ALL_STAGES),
    },
    "claude-opus-4-5-20251101": {
        "provenance": "Anthropic",
        "version": "opus-4.5",
        "stages": sorted(ALL_STAGES),
    },
    "claude-sonnet-5": {
        "provenance": "Anthropic",
        "version": "sonnet-5",
        "stages": sorted(ALL_STAGES),
    },
    "claude-sonnet-4-6": {
        "provenance": "Anthropic",
        "version": "sonnet-4.6",
        "stages": sorted(ALL_STAGES),
    },
    "claude-haiku-4-5-20251001": {
        "provenance": "Anthropic",
        "version": "haiku-4.5",
        # Haiku is a cheap reviewer/fixer, not approved to author specs/code.
        "stages": ["qa"],
    },
}


def load_registry() -> dict[str, dict[str, Any]]:
    """Return the active registry: ``AIFACTORY_MODEL_REGISTRY`` override (inline
    JSON or a file path) if present and valid, else :data:`DEFAULT_REGISTRY`.

    Fails safe: an unset/unreadable/invalid override yields the default, so a
    broken registry can never make a legitimate model look unapproved-and-denied.
    """
    raw = os.environ.get(ENV_REGISTRY, "").strip()
    if not raw:
        return DEFAULT_REGISTRY
    text = raw
    if not raw.startswith("{"):
        try:
            text = Path(raw).read_text(encoding="utf-8")
        except OSError:
            logger.warning("model registry file %r unreadable; using default", raw)
            return DEFAULT_REGISTRY
    try:
        data = json.loads(text)
    except ValueError:
        logger.warning("model registry is not valid JSON; using default")
        return DEFAULT_REGISTRY
    if not isinstance(data, dict):
        logger.warning("model registry is not a JSON object; using default")
        return DEFAULT_REGISTRY
    return data


def check_model_registered(model: str, stage: str) -> tuple[bool, str]:
    """Check ``model`` against the registry for ``stage``: ``(ok, reason)``.

    Only first-party Claude models are in scope; anything else (provider-prefixed
    local/third-party, or an unknown provider) returns ``(True, "")`` — the
    registry does not govern those. ``reason`` is empty on success.
    """
    # Lazy import: phase_config imports this module, so resolve at call time.
    from phase_config import (  # noqa: PLC0415
        infer_provider_from_model,
        resolve_model_id,
    )

    if infer_provider_from_model(model) != "claude":
        return True, ""

    model_id = resolve_model_id(model)
    registry = load_registry()
    entry = registry.get(model_id) or registry.get(model)
    if entry is None:
        return False, (
            f"model {model_id!r} (stage {stage!r}) is not in the approved model "
            f"registry — a model swap must pass the eval-gate and be registered "
            f"before use (see docs/docs/compliance/model-registry.md)"
        )
    stages = entry.get("stages")
    if isinstance(stages, list) and stage not in stages:
        return False, (
            f"model {model_id!r} is registered but not approved for stage "
            f"{stage!r} (approved stages: {sorted(stages)})"
        )
    return True, ""


def enforce_model_registry(model: str, stage: str) -> None:
    """Assert ``model`` is approved for ``stage`` per the enforcement mode.

    ``off`` skips; ``warn`` (default) logs; ``deny`` raises
    :class:`ModelNotApprovedError`. Any bug inside the check itself is swallowed
    (advisory) so a check failure can never break a build — only an explicit
    ``deny`` verdict blocks, and only in ``deny`` mode.
    """
    mode = os.environ.get(ENV_ENFORCE, "warn").strip().lower()
    if mode == "off":
        return
    try:
        ok, reason = check_model_registered(model, stage)
    except Exception:  # noqa: BLE001 - registry is advisory; never break a build
        return
    if ok:
        return
    if mode == "deny":
        raise ModelNotApprovedError(reason)
    logger.warning("model registry: %s", reason)
