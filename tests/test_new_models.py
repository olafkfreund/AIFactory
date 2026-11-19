"""New-model enablement (#2160): shorthands, legacy aliases, routing, tables."""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
sys.path.insert(0, str(BACKEND))

from phase_config import (  # noqa: E402
    ADAPTIVE_THINKING_MODELS,
    MODEL_ID_MAP,
    infer_provider_from_model,
    resolve_model_id,
)

NEW = {
    "fable": "claude-fable-5-2",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}
LEGACY = {
    "opus-4.8": "claude-opus-4-8",
    "opus-4.7": "claude-opus-4-7",
    "sonnet-5": "claude-sonnet-5",
    "sonnet-4.6": "claude-sonnet-4-6",
    "haiku-4.5": "claude-haiku-4-5-20251001",
}


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    for v in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        monkeypatch.delenv(v, raising=False)


@pytest.mark.parametrize("short,full", {**NEW, **LEGACY}.items())
def test_shorthands_and_legacy_aliases_resolve(short, full):
    assert resolve_model_id(short) == full


@pytest.mark.parametrize(
    "model,provider",
    [
        ("gpt-6.2-sol", "codex"),
        ("gpt-6.1-terra", "codex"),
        ("gpt-5.6-luna", "codex"),
        ("gemini-4-carbon-medium", "antigravity"),
        ("gemini-3.8-flash-high", "antigravity"),
        ("claude-sonnet-5-5", "claude"),
        ("fable", "claude"),
    ],
)
def test_provider_detection(model, provider):
    assert infer_provider_from_model(model) == provider


def test_every_new_id_is_priced_registered_and_cataloged():
    from core.enforcement import _CLAUDE_PRICING
    from model_registry import DEFAULT_REGISTRY

    catalog = json.loads((BACKEND / "core" / "model-catalog.json").read_text())[
        "models"
    ]
    for full in [*NEW.values(), *LEGACY.values(), *MODEL_ID_MAP.values()]:
        assert full in _CLAUDE_PRICING, full
        assert full in DEFAULT_REGISTRY, full
    for full in NEW.values():
        assert full in catalog, full
    assert {
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-2",
    } <= ADAPTIVE_THINKING_MODELS


def test_budget_shorthands_follow_phase_config():
    from core.budget_enforcement import _SHORTHAND_TO_CATALOG

    assert set(_SHORTHAND_TO_CATALOG) == set(MODEL_ID_MAP)
    for short, full in _SHORTHAND_TO_CATALOG.items():
        assert resolve_model_id(short) == full
