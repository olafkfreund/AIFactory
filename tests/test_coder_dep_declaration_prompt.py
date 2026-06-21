"""Coder prompts must tell the agent to declare test dependencies (#611 f).

The 2026-06-18 taskboard demo shipped an unrunnable build: the generated
`requirements.txt` omitted `httpx`, which FastAPI's `TestClient` needs but
`fastapi` does not pull in. These pin the prompt guidance that prevents it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PROMPTS = Path(__file__).parent.parent / "apps" / "backend" / "prompts"


@pytest.mark.parametrize("name", ["coder.md", "coder_story_enhanced.md"])
def test_coder_prompt_requires_declaring_test_deps(name: str) -> None:
    text = (_PROMPTS / name).read_text(encoding="utf-8")
    lower = text.lower()
    # Names the canonical trap (httpx + TestClient) and the rule (declare deps).
    assert "httpx" in lower, f"{name} should call out the httpx/TestClient trap"
    assert "testclient" in lower
    assert "declare" in lower
    # Mentions the manifest the dep must land in.
    assert "requirements.txt" in lower or "manifest" in lower
    # Ties it to runtime AND test deps (not just runtime).
    assert "test" in lower
