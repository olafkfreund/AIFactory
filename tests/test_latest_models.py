"""Guard: default model IDs stay on the latest version per provider.

Bump these intentionally when a newer model ships — the point is that the
defaults don't silently lag (e.g. Gemini stuck on 2.5-pro, opus on 4.7).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))


def test_claude_shorthands_resolve_to_latest():
    from phase_config import resolve_model_id

    assert resolve_model_id("opus") == "claude-opus-4-8"
    assert resolve_model_id("sonnet") == "claude-sonnet-4-6"
    assert resolve_model_id("haiku") == "claude-haiku-4-5-20251001"
    # previous flagship still pinnable
    assert resolve_model_id("opus-4.7") == "claude-opus-4-7"


def test_default_phase_models_use_latest_sonnet():
    from phase_config import DEFAULT_PHASE_MODELS, resolve_model_id

    assert {resolve_model_id(m) for m in DEFAULT_PHASE_MODELS.values()} == {
        "claude-sonnet-4-6"
    }


def test_antigravity_agentic_default_is_latest():
    import providers.antigravity_agentic as g

    # Newest model validated on the Antigravity/Gemini CLI (2026-06-09).
    assert g._DEFAULT_MODEL == "gemini-3.5-flash"


def test_gemini_agentic_shim_still_resolves():
    # Back-compat: the legacy import path must keep working.
    import providers.antigravity_agentic as ag
    import providers.gemini_agentic as g

    assert g.GeminiAgenticProvider is ag.AntigravityAgenticProvider
