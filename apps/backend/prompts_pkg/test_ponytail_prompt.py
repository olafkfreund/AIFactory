"""Tests for the ponytail minimal-code prompt injection.

Verifies the ladder reaches every build agent (planner, coder, solo), the
reviewer-side note reaches QA, and the injected text stays provider-neutral so
it behaves identically across Claude, Sonnet/Fable, local Ollama, and Codex.
"""

from __future__ import annotations

from pathlib import Path

from prompts_pkg.prompts import (
    PONYTAIL_BUILD_CONTEXT,
    PONYTAIL_QA_CONTEXT,
    get_coding_prompt,
    get_planner_prompt,
    get_qa_reviewer_prompt,
    get_solo_prompt,
)

# The injected text must never name a model or vendor — that is what makes it
# usable by every LLM supplier.
_PROVIDER_NAMES = [
    "claude",
    "anthropic",
    "gpt",
    "openai",
    "codex",
    "copilot",
    "gemini",
    "ollama",
    "llama",
    "mistral",
]


def test_build_context_has_ladder_and_safety_carveout() -> None:
    text = PONYTAIL_BUILD_CONTEXT.lower()
    assert "does this need to exist at all?" in text  # rung 1
    assert "standard library" in text  # rung 3
    assert "one line" in text  # rung 6
    # Safety must be explicitly protected, or "minimal" becomes "unsafe".
    assert "validation at trust boundaries" in text
    assert "security" in text
    assert "accessibility" in text


def test_injected_text_is_provider_neutral() -> None:
    for blob in (PONYTAIL_BUILD_CONTEXT, PONYTAIL_QA_CONTEXT):
        low = blob.lower()
        for name in _PROVIDER_NAMES:
            assert name not in low, f"ponytail block names a provider: {name!r}"


def test_build_agents_receive_the_ladder(tmp_path: Path) -> None:
    for builder in (get_planner_prompt, get_coding_prompt, get_solo_prompt):
        prompt = builder(tmp_path)
        assert "MINIMAL-CODE DISCIPLINE (PONYTAIL" in prompt, builder.__name__


def test_solo_also_gets_reviewer_note(tmp_path: Path) -> None:
    # Solo is coder + QA in one flow, so it must get both blocks.
    prompt = get_solo_prompt(tmp_path)
    assert "REVIEWING MINIMAL CODE (PONYTAIL-AWARE)" in prompt


def test_qa_reviewer_is_ponytail_aware(tmp_path: Path) -> None:
    prompt = get_qa_reviewer_prompt(tmp_path, tmp_path)
    assert "REVIEWING MINIMAL CODE (PONYTAIL-AWARE)" in prompt
