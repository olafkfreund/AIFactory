"""A suite of nothing but skips is not verification.

A gate whose tool is missing is reported `skipped`, and `summarize_gates`
renders an all-skipped suite as a pass. Seen live on task
`006-ac-prof-003-02-biography-over-`: the build Job did not carry the sandbox
env, so every gate fell to a plain host subprocess with no toolchain and the
run recorded `kotlin-unit: skipped, swift-unit: skipped` — the same empty
result as "no gates detected", wearing the word `passed` (AIFactory#1491).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

from cli.build_commands import _evidence_shows_an_executed_gate  # noqa: E402


def test_an_all_skipped_suite_is_not_evidence():
    assert not _evidence_shows_an_executed_gate(
        "kotlin-unit: skipped, swift-unit: skipped"
    )


def test_one_executed_gate_among_skips_is_evidence():
    # A real result for any gate means something ran; the skips beside it are
    # honest absences, not a reason to discard the run.
    assert _evidence_shows_an_executed_gate("kotlin-unit: passed, swift-unit: skipped")


def test_a_failing_gate_is_still_evidence_that_one_ran():
    assert _evidence_shows_an_executed_gate("kotlin-unit: failed")


def test_no_gates_detected_is_not_evidence():
    assert not _evidence_shows_an_executed_gate("no gates detected in /work")


def test_a_missing_marker_is_not_evidence():
    assert not _evidence_shows_an_executed_gate(None)
    assert not _evidence_shows_an_executed_gate("")


def test_unparseable_evidence_is_not_evidence():
    # Better to call an unreadable summary unverified than to read a pass into it.
    assert not _evidence_shows_an_executed_gate("something entirely unexpected")
