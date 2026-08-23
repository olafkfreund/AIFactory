"""The fire-once marker must record delivery, not the attempt (#1407).

The marker was written BEFORE the emit, and the emit only runs when the marker
is absent. So one failure -- transient or not -- wrote a permanent "already
done" tombstone and every later call skipped the emit entirely. Nothing retried
and nothing complained, because the failure logged at debug in a pod running at
info.

Observed: 6 of 7 specs carried `.terminal_completion_emitted` while CFactory had
received 2 POSTs in 24 hours, one of them a hand-sent probe. Every cost and
token figure in the cockpit read zero, for every run.

These tests are about the ORDER of the marker write relative to delivery, which
is the whole defect. A test that only checks "emit was called" passes against
the broken code on the first call and never exercises the second.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

MARKER = ".terminal_completion_emitted"


@pytest.fixture
def spec(tmp_path: Path) -> Path:
    d = tmp_path / "001-demo"
    d.mkdir()
    return d


def _run(spec_dir: Path, monkeypatch, *, delivered: bool, calls: list):
    """Drive run_terminal_completion with the emitter stubbed."""
    import asyncio

    from server.services import completion_orchestration as orch

    def fake_emit(sd, *, task_id, project_id, spec_id, status):
        calls.append(spec_id)
        return {"_delivered": delivered}

    monkeypatch.setattr(
        "server.services.completion.emit_terminal_completion", fake_emit
    )

    asyncio.run(
        orch.run_terminal_completion(
            spec_dir=spec_dir,
            project_path=spec_dir.parent,
            spec_id=spec_dir.name,
            task_id=f"p:{spec_dir.name}",
            backend_path=None,
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=__import__("logging").getLogger("t"),
        )
    )


def test_a_failed_delivery_leaves_no_marker(spec: Path, monkeypatch) -> None:
    """The tombstone case. No marker means the next call retries."""
    calls: list = []
    _run(spec, monkeypatch, delivered=False, calls=calls)

    assert calls == [spec.name], "the emit must have been attempted"
    assert not (spec / MARKER).exists(), (
        "a failed delivery must not leave a marker -- it would suppress every "
        "later attempt permanently (#1407)"
    )


def test_a_failed_delivery_is_retried_on_the_next_call(spec: Path, monkeypatch) -> None:
    """The property that actually matters, and the one a single-call test misses.

    Against the broken code the second call is skipped, because the marker was
    already written by the first.
    """
    calls: list = []
    _run(spec, monkeypatch, delivered=False, calls=calls)
    _run(spec, monkeypatch, delivered=False, calls=calls)

    assert len(calls) == 2, (
        f"expected a retry, got {len(calls)} attempt(s) -- the first failure "
        "suppressed the second"
    )


def test_a_successful_delivery_writes_the_marker(spec: Path, monkeypatch) -> None:
    calls: list = []
    _run(spec, monkeypatch, delivered=True, calls=calls)

    assert (spec / MARKER).exists(), "a delivered event must be marked"
    assert (spec / MARKER).read_text().strip(), "the marker should carry a timestamp"


def test_a_successful_delivery_is_not_re_sent(spec: Path, monkeypatch) -> None:
    """Fire-once must still hold for the case it was written for."""
    calls: list = []
    _run(spec, monkeypatch, delivered=True, calls=calls)
    _run(spec, monkeypatch, delivered=True, calls=calls)

    assert len(calls) == 1, "a delivered event must not be emitted twice"


def test_an_emitter_without_the_flag_is_treated_as_delivered(
    spec: Path, monkeypatch
) -> None:
    """Back-compat: an event dict with no `_delivered` key must not block marking.

    Defaulting the other way would make every caller that has not been updated
    re-send forever.
    """
    import asyncio
    import logging

    from server.services import completion_orchestration as orch

    monkeypatch.setattr(
        "server.services.completion.emit_terminal_completion",
        lambda sd, **kw: {},
    )
    asyncio.run(
        orch.run_terminal_completion(
            spec_dir=spec,
            project_path=spec.parent,
            spec_id=spec.name,
            task_id=f"p:{spec.name}",
            backend_path=None,
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=logging.getLogger("t"),
        )
    )

    assert (spec / MARKER).exists()
