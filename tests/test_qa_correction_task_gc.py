"""GC regression for the QA-fixer background task (#1484).

``qa.correction._default_fixer`` answers ``{"status": "qa_fixing",
"scheduled": True}`` and lets the fixer run on. Before #1484 it did that with a
bare ``asyncio.create_task(...)``: the loop keeps only a WEAK reference, so a
collection between two awaits could reclaim the fixer mid-run — no exception,
no log, and a caller already told the fix was scheduled.

The test forces ``gc.collect()`` while the fixer is suspended and asserts it
still finished. See apps/web-server/tests/test_background_task_gc.py for why
the gate future is published through a weak reference rather than awaited on an
Event (an Event's waiter would anchor the task by itself, and the test would
pass with or without the fix).

MUTATION-VERIFIED: dropping the ``_BACKGROUND_FIXERS`` anchor in
``qa/correction.py`` makes this test fail.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from qa import correction


async def test_the_scheduled_fixer_survives_a_gc_cycle(tmp_path: Path) -> None:
    box: weakref.WeakValueDictionary[str, asyncio.Future[None]] = (
        weakref.WeakValueDictionary()
    )
    ran: list[str] = []

    async def fake_fixer(spec_dir: Path) -> None:
        gate = asyncio.get_running_loop().create_future()
        box["gate"] = gate  # only a WEAK handle escapes this frame
        await gate
        ran.append(str(spec_dir))

    original = correction._run_fixer_bg
    correction._run_fixer_bg = fake_fixer  # type: ignore[assignment]
    try:
        result = await correction._default_fixer(tmp_path)
        assert result == {"status": "qa_fixing", "scheduled": True}

        await asyncio.sleep(0)  # let the fixer start and suspend
        gc.collect()

        gate = box.get("gate")
        assert gate is not None, (
            "the QA fixer was garbage-collected after _default_fixer already "
            "answered scheduled=True"
        )
        gate.set_result(None)
        await asyncio.sleep(0)
        assert ran == [str(tmp_path)]
    finally:
        correction._run_fixer_bg = original  # type: ignore[assignment]
