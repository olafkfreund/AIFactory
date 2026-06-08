"""Tests for the completion-event outbox + retrying relay (#465).

The headline guarantee — *killing the emitter between the state change and a
successful POST results in eventual delivery, with no lost event* — is exercised
by ``test_crash_before_delivery_is_eventually_delivered``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from server.services import outbox  # noqa: E402
from server.services.completion import build_completion_event, notify_completion  # noqa: E402


def _event(event_id="11111111-1111-4111-8111-111111111111", status="done"):
    return build_completion_event(
        task_id="proj:spec-9", spec_id="spec-9", status=status, issue_number=412,
        event_id=event_id,
    )


def _db(tmp_path) -> Path:
    return tmp_path / "outbox.db"


# ── enqueue ──────────────────────────────────────────────────────────────────


def test_enqueue_persists_a_pending_row(tmp_path):
    db = _db(tmp_path)
    assert outbox.enqueue(_event(), "http://hook.test/c", path=db, now=1000.0) is True
    assert outbox.pending_count(path=db) == 1


def test_enqueue_is_idempotent_on_event_id(tmp_path):
    """Re-delivery dedup hinges on a stable id (#466): re-enqueue is a no-op."""
    db = _db(tmp_path)
    ev = _event()
    assert outbox.enqueue(ev, "http://hook.test/c", path=db, now=1000.0) is True
    assert outbox.enqueue(ev, "http://hook.test/c", path=db, now=1001.0) is False
    assert outbox.pending_count(path=db) == 1


def test_enqueue_without_id_is_skipped(tmp_path):
    db = _db(tmp_path)
    assert outbox.enqueue({"correlation_key": "x"}, "http://hook.test/c", path=db) is False
    assert outbox.pending_count(path=db) == 0


# ── delivery ─────────────────────────────────────────────────────────────────


def test_deliver_due_marks_delivered_on_success(tmp_path):
    db = _db(tmp_path)
    sent = []
    outbox.enqueue(_event(), "http://hook.test/c", path=db, now=1000.0)

    res = outbox.deliver_due_once(
        path=db, now=1000.0, sender=lambda url, payload: sent.append((url, payload))
    )
    assert res == {"delivered": 1, "failed": 0, "abandoned": 0, "remaining": 0}
    assert outbox.pending_count(path=db) == 0
    assert sent[0][0] == "http://hook.test/c"
    assert json.loads(sent[0][1])["correlation_key"] == "412"


def test_failed_delivery_stays_pending_and_backs_off(tmp_path):
    db = _db(tmp_path)
    outbox.enqueue(_event(), "http://hook.test/c", path=db, now=1000.0)

    def _boom(url, payload):
        raise outbox.DeliveryError("refused")

    res = outbox.deliver_due_once(path=db, now=1000.0, sender=_boom)
    assert res["failed"] == 1 and res["delivered"] == 0
    assert outbox.pending_count(path=db) == 1  # not lost — still queued

    # Backed off: not due again at the same instant, so a retry now does nothing.
    res2 = outbox.deliver_due_once(path=db, now=1000.0, sender=_boom)
    assert res2["delivered"] == 0 and res2["failed"] == 0


def test_crash_before_delivery_is_eventually_delivered(tmp_path):
    """The core at-least-once guarantee (AC #465).

    Enqueue (the durable write that happens *with* the terminal state change),
    then simulate the emitter dying mid-POST: the first relay tick's sender
    raises. A later tick — after backoff — succeeds, and the event is delivered
    exactly the once it should be, never lost.
    """
    db = _db(tmp_path)
    outbox.enqueue(_event(), "http://hook.test/c", path=db, now=1000.0)

    # Tick 1 — the "POST" blows up (process would have crashed here).
    outbox.deliver_due_once(
        path=db, now=1000.0,
        sender=lambda u, p: (_ for _ in ()).throw(outbox.DeliveryError("crash")),
    )
    assert outbox.pending_count(path=db) == 1

    # Tick 2 — backoff elapsed (>=5s), target healthy again.
    delivered = []
    res = outbox.deliver_due_once(
        path=db, now=1010.0, sender=lambda u, p: delivered.append(p)
    )
    assert res["delivered"] == 1
    assert outbox.pending_count(path=db) == 0
    assert len(delivered) == 1  # delivered once — not lost, not duplicated locally


def test_backoff_schedule_is_exponential_and_capped():
    assert outbox._backoff_seconds(0) == 0.0
    assert outbox._backoff_seconds(1) == 5.0
    assert outbox._backoff_seconds(2) == 10.0
    assert outbox._backoff_seconds(3) == 20.0
    assert outbox._backoff_seconds(100) == 3600.0  # capped at 1h


def test_event_abandoned_after_max_attempts(tmp_path):
    db = _db(tmp_path)
    outbox.enqueue(_event(), "http://hook.test/c", path=db, now=0.0)

    def _boom(url, payload):
        raise outbox.DeliveryError("permanently down")

    now = 0.0
    last = {}
    for _ in range(outbox._MAX_ATTEMPTS + 2):
        last = outbox.deliver_due_once(path=db, now=now, sender=_boom)
        now += outbox._BACKOFF_CAP_S * 48  # always past the next attempt window
    assert last["abandoned"] >= 1
    # Abandoned rows stop being retried (pushed far into the future).
    assert outbox.deliver_due_once(path=db, now=now, sender=_boom)["failed"] == 0


# ── completion.py integration: flag routes through the outbox ─────────────────


def test_notify_completion_enqueues_when_flag_on(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setenv("AIFACTORY_COMPLETION_OUTBOX", "true")
    monkeypatch.setenv("AIFACTORY_COMPLETION_OUTBOX_DB", str(db))
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK", "http://hook.test/c")
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)

    # Direct POST must NOT happen when the outbox is on.
    import urllib.request

    def _no_post(req, timeout=None):
        raise AssertionError("direct POST should not run when outbox is enabled")

    monkeypatch.setattr(urllib.request, "urlopen", _no_post)

    notify_completion(_event())
    assert outbox.pending_count(path=db) == 1


def test_notify_completion_direct_post_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_COMPLETION_OUTBOX", raising=False)
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK", "http://hook.test/c")
    monkeypatch.delenv("AIFACTORY_COMPLETION_SENTINEL", raising=False)
    posted = {}

    class _Resp:
        def close(self):
            pass

    def _fake(req, timeout=None):
        posted["url"] = req.full_url
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    notify_completion(_event())
    assert posted["url"] == "http://hook.test/c"  # legacy path intact
