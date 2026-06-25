"""parse_insights JSON-extraction tolerance for reasoning-field models.

Ollama gemma4 / gpt-oss preface their JSON with prose or a thinking preamble and
then emit a fenced block; the extractor must still recover the JSON (was: a
preamble-then-fence response logged "Failed to parse insights JSON").
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from analysis.insight_extractor import parse_insights  # noqa: E402

_BODY = '{"file_insights": [{"file": "a.go", "summary": "s"}], "patterns": []}'


def test_direct_json() -> None:
    assert parse_insights(_BODY)["file_insights"][0]["file"] == "a.go"


def test_fenced_at_start() -> None:
    assert parse_insights(f"```json\n{_BODY}\n```")["patterns"] == []


def test_reasoning_preamble_then_fenced_block() -> None:
    # The gemma4 shape: thinking/prose, THEN a fenced JSON block.
    text = (
        "Let me analyze the session.\n\nThe agent created one Go file.\n\n"
        f"```json\n{_BODY}\n```\n\nThat is the structured result."
    )
    out = parse_insights(text)
    assert out is not None and out["file_insights"][0]["file"] == "a.go"


def test_brace_matching_fallback() -> None:
    # No fence, JSON embedded in prose → first { … last } recovers it.
    assert parse_insights(f"Here is the result: {_BODY} done.") is not None


def test_prose_without_json_returns_none() -> None:
    assert parse_insights("I was unable to analyze the session.") is None


def test_empty_returns_none() -> None:
    assert parse_insights("   ") is None
