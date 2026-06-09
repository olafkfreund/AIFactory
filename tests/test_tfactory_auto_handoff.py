"""#496: opt-in auto-handover of a finished task to TFactory for testing.

`maybe_auto_handoff_tfactory` fires only when task_metadata has
`auto_handover_tfactory` set; it builds the handoff payload from the spec's
requirements + meta and calls send_handoff. Best-effort; never raises.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from pfactory import tfactory_client as tc  # noqa: E402


def _spec(tmp: Path, opt_in: bool) -> Path:
    meta = {"auto_handover_tfactory": True} if opt_in else {}
    (tmp / "task_metadata.json").write_text(json.dumps(meta))
    (tmp / "requirements.json").write_text(json.dumps({"title": "t", "description": "d"}))
    return tmp


def test_wants_auto_handoff(tmp_path):
    assert tc.wants_auto_handoff(_spec(tmp_path, True)) is True
    (tmp_path / "task_metadata.json").write_text("{}")
    assert tc.wants_auto_handoff(tmp_path) is False
    assert tc.wants_auto_handoff(tmp_path / "does-not-exist") is False


def test_not_requested_is_noop(tmp_path):
    _spec(tmp_path, False)
    result = asyncio.run(tc.maybe_auto_handoff_tfactory(tmp_path, "001-x"))
    assert result == {"sent": False, "reason": "not_requested"}


def test_opted_in_sends_and_records(tmp_path, monkeypatch):
    _spec(tmp_path, True)
    captured = {}

    async def fake_send(payload, **kwargs):
        captured["payload"] = payload
        return {"sent": True, "reason": None, "status": 200}

    monkeypatch.setattr(tc, "send_handoff", fake_send)

    result = asyncio.run(tc.maybe_auto_handoff_tfactory(tmp_path, "001-x"))
    assert result["sent"] is True
    assert captured["payload"]["spec_id"] == "001-x"
    assert captured["payload"]["source"] == "aifactory"
    # Outcome marker written for the UI/operator.
    assert (tmp_path / "tfactory_handoff.json").exists()


def test_never_raises_on_bad_input(tmp_path, monkeypatch):
    _spec(tmp_path, True)

    async def boom(payload, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(tc, "send_handoff", boom)
    result = asyncio.run(tc.maybe_auto_handoff_tfactory(tmp_path, "001-x"))
    assert result["sent"] is False and result["reason"] == "error"
