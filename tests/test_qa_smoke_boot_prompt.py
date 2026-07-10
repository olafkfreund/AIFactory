"""QA reviewer must smoke-boot the artifact, not just run pytest (#611 d).

The 2026-06-18 taskboard demo passed pytest against an app assembled only in
`conftest` while there was no runnable entrypoint, and escalated to human
review. These pin the reviewer-prompt guidance that turns that into a REJECT.
"""

from __future__ import annotations

from pathlib import Path

_QA_REVIEWER = (
    Path(__file__).parent.parent / "apps" / "backend" / "prompts" / "qa_reviewer.md"
)


def test_qa_reviewer_requires_smoke_boot() -> None:
    text = (_QA_REVIEWER).read_text(encoding="utf-8")
    lower = text.lower()
    # Has a dedicated smoke-boot phase.
    assert "smoke-boot" in lower
    # Boots the real entrypoint and probes the running service.
    assert "entrypoint" in lower
    assert "/health" in lower
    # Names the exact trap: pytest passing against a conftest-only app.
    assert "conftest" in lower
    # No runnable entrypoint must be a rejection, not an approval.
    assert "reject" in lower
