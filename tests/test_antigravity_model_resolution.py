"""Antigravity provider model resolution.

The bare provider selector `antigravity` is NOT a real Gemini model — passing
it as `--model antigravity` yields `ModelNotFoundError: models/antigravity` and
the CLI exits 1 (build fails with no completed subtask). The provider must map
bare selectors to the default model, and never emit `--model antigravity`.
Default is the newest validated model, gemini-3.5-flash.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from providers.antigravity_agentic import (  # noqa: E402
    _DEFAULT_MODEL,
    AntigravityAgenticProvider,
    _resolve_model,
)


def test_default_model_is_newer():
    assert _DEFAULT_MODEL == "gemini-3.5-flash"


def test_named_selectors_map_to_default():
    for sel in ("antigravity", "default", "antigravity-default"):
        assert _resolve_model(sel) == _DEFAULT_MODEL, sel


def test_empty_omits_model():
    # Empty/None → "" so the provider omits --model (CLI uses its own default).
    assert _resolve_model("") == ""
    assert _resolve_model(None) == ""
    assert _resolve_model("   ") == ""


def test_prefix_stripped():
    assert _resolve_model("antigravity:gemini-3.5-flash") == "gemini-3.5-flash"
    assert _resolve_model("antigravity:") == _DEFAULT_MODEL


def test_concrete_models_pass_through():
    assert _resolve_model("gemini-2.5-pro") == "gemini-2.5-pro"
    assert _resolve_model("gemini-3.1-pro-preview") == "gemini-3.1-pro-preview"


def test_provider_never_emits_model_antigravity():
    """The bug: --model antigravity. Building the command must not contain it."""
    p = AntigravityAgenticProvider(model="antigravity")
    cmd = p._build_command()
    assert "antigravity" not in cmd[cmd.index("--model") + 1] if "--model" in cmd else True
    # And the resolved model is the default
    assert p._model == _DEFAULT_MODEL
