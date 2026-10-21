"""#1619 — a task timestamp must carry its timezone.

`_iso_from_timestamp` rendered the server's local clock with no offset. The
cockpit parses it with `new Date(...)`, and JavaScript reads an offset-less ISO
string as the *browser's* local time. The pod runs UTC, the operator was on
BST — so a task dispatched 32 minutes earlier rendered "1h ago", and anything
deriving staleness from it was an hour out.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.routes.task_service import _iso_from_timestamp  # noqa: E402


def test_timestamp_is_timezone_aware():
    """THE regression: no offset meant the reader had to guess, and guessed wrong."""
    parsed = datetime.fromisoformat(_iso_from_timestamp(1_800_000_000.0))
    assert parsed.tzinfo is not None, "an offset-less ISO string is read as local time"


def test_timestamp_is_the_same_instant_in_utc():
    """The value must not merely gain an offset — it must still be the right moment."""
    epoch_seconds = 1_800_000_000.0
    parsed = datetime.fromisoformat(_iso_from_timestamp(epoch_seconds))
    assert parsed.timestamp() == epoch_seconds
    assert (
        parsed.astimezone(UTC)
        .isoformat()
        .startswith(datetime.fromtimestamp(epoch_seconds, UTC).isoformat()[:19])
    )


def test_a_utc_reader_sees_no_offset_shift():
    """An aware value compares correctly against an aware 'now'.

    CFactory's reader falls back to ``datetime.now(UTC)``; a naive value here
    made that comparison a TypeError or an hour-wrong answer depending on path.
    """
    now = datetime.now(UTC)
    rendered = datetime.fromisoformat(_iso_from_timestamp(now.timestamp()))
    assert abs((now - rendered).total_seconds()) < 1.0
