"""
Tests for durable file-based agent inbox messaging (#264).

Covers:
- Atomic write (no half-written/corrupt files visible to readers)
- messageId read-back delivery verification
- enqueue → read → mark-read lifecycle
- concurrent-write safety (no corruption, no lost messages)
- backend consumption-side drain (exactly-once, prompt formatting)
- shared on-disk contract between web-server writer and backend reader
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

# The web-server (writer) and backend (reader) live in separate apps. Tests
# exercise both sides against the same on-disk schema.
_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Backend reader is in apps/backend/agents/inbox.py. Import the module file
# directly to avoid pulling the whole agents package (and its SDK deps).
import importlib.util  # noqa: E402

from server.services import inbox_service  # noqa: E402

_reader_path = _BACKEND / "agents" / "inbox.py"
_spec = importlib.util.spec_from_file_location("agent_inbox_reader", _reader_path)
agent_inbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_inbox)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    """A spec directory in which the inbox/ subdir will be created."""
    d = tmp_path / "001-feature"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# Schema / basic enqueue
# ---------------------------------------------------------------------------


def test_enqueue_creates_well_formed_message(spec_dir: Path):
    result = inbox_service.enqueue(spec_dir, text="stop and run the tests")

    assert result["delivered"] is True
    assert result["recipient"] == "agent"
    assert result["messageId"]

    msg = result["message"]
    for field in ("from", "text", "summary", "timestamp", "messageId", "read"):
        assert field in msg, f"missing schema field: {field}"
    assert msg["read"] is False
    assert msg["text"] == "stop and run the tests"
    assert msg["summary"] == "stop and run the tests"


def test_enqueue_writes_json_array_at_expected_location(spec_dir: Path):
    inbox_service.enqueue(spec_dir, text="hello")
    inbox_path = spec_dir / "inbox" / "agent.json"
    assert inbox_path.exists()
    data = json.loads(inbox_path.read_text())
    assert isinstance(data, list)
    assert len(data) == 1


def test_empty_text_rejected(spec_dir: Path):
    with pytest.raises(inbox_service.InboxError):
        inbox_service.enqueue(spec_dir, text="   ")


def test_recipient_sanitized_against_traversal(spec_dir: Path):
    inbox_service.enqueue(spec_dir, text="x", recipient="../../etc/passwd")
    # Must stay inside spec_dir/inbox with a flattened, safe name.
    files = list((spec_dir / "inbox").glob("*.json"))
    assert len(files) == 1
    assert ".." not in files[0].name
    assert files[0].parent == spec_dir / "inbox"


# ---------------------------------------------------------------------------
# Delivery verification (messageId read-back)
# ---------------------------------------------------------------------------


def test_delivery_verification_reads_back_messageid(spec_dir: Path):
    result = inbox_service.enqueue(spec_dir, text="verify me")
    persisted = json.loads((spec_dir / "inbox" / "agent.json").read_text())
    ids = {m["messageId"] for m in persisted}
    assert result["messageId"] in ids
    assert result["delivered"] is True


def test_delivery_verification_failure_raises(spec_dir: Path, monkeypatch):
    # Simulate a write that does not persist the message: stub the atomic
    # writer to a no-op so read-back cannot find the id.
    monkeypatch.setattr(inbox_service, "_write_messages_atomic", lambda *a, **k: None)
    with pytest.raises(inbox_service.DeliveryVerificationError):
        inbox_service.enqueue(spec_dir, text="will not persist")


# ---------------------------------------------------------------------------
# Atomic write integrity
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_tmp_files(spec_dir: Path):
    for i in range(5):
        inbox_service.enqueue(spec_dir, text=f"msg {i}")
    leftover = list((spec_dir / "inbox").glob(".*.tmp.*"))
    assert leftover == [], f"temp files leaked: {leftover}"


def test_reader_never_sees_partial_file(spec_dir: Path):
    # Every committed file must be valid JSON (atomic replace guarantees this).
    inbox_service.enqueue(spec_dir, text="a")
    inbox_service.enqueue(spec_dir, text="b")
    raw = (spec_dir / "inbox" / "agent.json").read_text()
    json.loads(raw)  # must not raise


# ---------------------------------------------------------------------------
# enqueue → read → mark-read lifecycle
# ---------------------------------------------------------------------------


def test_full_lifecycle(spec_dir: Path):
    r1 = inbox_service.enqueue(spec_dir, text="first")
    r2 = inbox_service.enqueue(spec_dir, text="second")

    pending = inbox_service.read_pending(spec_dir)
    assert len(pending) == 2

    updated = inbox_service.mark_read(spec_dir, [r1["messageId"]])
    assert updated == 1

    pending = inbox_service.read_pending(spec_dir)
    assert len(pending) == 1
    assert pending[0]["messageId"] == r2["messageId"]

    # Marking an already-read id again is a no-op.
    assert inbox_service.mark_read(spec_dir, [r1["messageId"]]) == 0

    # All messages still listable.
    assert len(inbox_service.list_messages(spec_dir)) == 2


def test_drain_unread_marks_read_exactly_once(spec_dir: Path):
    inbox_service.enqueue(spec_dir, text="one")
    inbox_service.enqueue(spec_dir, text="two")

    drained = inbox_service.drain_unread(spec_dir)
    assert len(drained) == 2

    # Second drain returns nothing (already marked read).
    assert inbox_service.drain_unread(spec_dir) == []


# ---------------------------------------------------------------------------
# Concurrency safety
# ---------------------------------------------------------------------------


def test_concurrent_enqueue_no_lost_or_corrupt_messages(spec_dir: Path):
    n_threads = 12
    per_thread = 8
    errors: list[Exception] = []

    def worker(tid: int):
        try:
            for i in range(per_thread):
                inbox_service.enqueue(spec_dir, text=f"t{tid}-m{i}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"enqueue errors: {errors}"

    # File must be valid JSON and contain every message exactly once.
    raw = (spec_dir / "inbox" / "agent.json").read_text()
    messages = json.loads(raw)
    assert len(messages) == n_threads * per_thread
    ids = {m["messageId"] for m in messages}
    assert len(ids) == n_threads * per_thread  # all unique, none lost


def test_concurrent_enqueue_and_drain(spec_dir: Path):
    """Concurrent writers + a draining reader never corrupt the file."""
    stop = threading.Event()
    drained_ids: set[str] = set()
    errors: list[Exception] = []

    def writer():
        try:
            for i in range(40):
                inbox_service.enqueue(spec_dir, text=f"w-{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                for m in inbox_service.drain_unread(spec_dir):
                    drained_ids.add(m["messageId"])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    rt.start()
    wt.start()
    wt.join()
    stop.set()
    rt.join()
    # Final sweep for anything left unread.
    for m in inbox_service.drain_unread(spec_dir):
        drained_ids.add(m["messageId"])

    assert not errors, f"errors: {errors}"
    all_ids = {m["messageId"] for m in inbox_service.list_messages(spec_dir)}
    assert drained_ids == all_ids  # every message drained exactly once


# ---------------------------------------------------------------------------
# Backend reader (consumption side) + shared contract
# ---------------------------------------------------------------------------


def test_backend_reader_drains_messages_written_by_web_server(spec_dir: Path):
    """The backend reader consumes what the web-server writer produced."""
    inbox_service.enqueue(spec_dir, text="run the linter", sender="user")
    inbox_service.enqueue(spec_dir, text="then commit", sender="user")

    assert agent_inbox.has_pending(spec_dir) is True

    drained = agent_inbox.drain_unread(spec_dir)
    assert len(drained) == 2
    assert [m["text"] for m in drained] == ["run the linter", "then commit"]

    # Exactly-once: a second drain (either side) yields nothing.
    assert agent_inbox.drain_unread(spec_dir) == []
    assert inbox_service.read_pending(spec_dir) == []
    assert agent_inbox.has_pending(spec_dir) is False


def test_backend_reader_empty_inbox_is_safe(spec_dir: Path):
    assert agent_inbox.drain_unread(spec_dir) == []
    assert agent_inbox.has_pending(spec_dir) is False


def test_backend_format_for_prompt(spec_dir: Path):
    inbox_service.enqueue(spec_dir, text="please add tests", sender="user")
    drained = agent_inbox.drain_unread(spec_dir)
    rendered = agent_inbox.format_for_prompt(drained)
    assert "Incoming user messages" in rendered
    assert "please add tests" in rendered
    assert agent_inbox.format_for_prompt([]) == ""


def test_backend_reader_tolerates_corrupt_file(spec_dir: Path):
    """A corrupt inbox must not crash the build; drain returns []."""
    inbox_path = spec_dir / "inbox" / "agent.json"
    inbox_path.parent.mkdir(parents=True)
    inbox_path.write_text("{ this is not valid json")
    assert agent_inbox.drain_unread(spec_dir) == []
    assert agent_inbox.has_pending(spec_dir) is False
