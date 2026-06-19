"""qa_fixer must auto-repair mechanical defects, not escalate them (#611 e).

The taskboard demo escalated on deterministic, fixable problems (no runnable
entrypoint, a dependency missing from the manifest). These pin the fixer-prompt
guidance that turns those into direct repairs.
"""

from __future__ import annotations

from pathlib import Path

_QA_FIXER = (
    Path(__file__).parent.parent / "apps" / "backend" / "prompts" / "qa_fixer.md"
)


def test_qa_fixer_repairs_mechanical_defects() -> None:
    lower = _QA_FIXER.read_text(encoding="utf-8").lower()
    # Names the mechanical defects it must fix itself.
    assert "entrypoint" in lower
    assert "httpx" in lower or "manifest" in lower
    # Frames them as repair-not-escalate.
    assert "escalate" in lower
    assert "mechanical" in lower or "deterministic" in lower
    # Points at the dependency manifest as the place to add deps.
    assert "requirements.txt" in lower or "manifest" in lower
