"""Strong references for fire-and-forget background tasks (#1484).

``asyncio.create_task`` hands the task to the event loop, but the loop keeps
only a WEAK reference to it. If the caller drops its own reference — the usual
shape being ``asyncio.create_task(coro())`` on a line of its own, immediately
before returning a success response — a garbage-collection cycle between two
awaits can collect the task mid-flight. There is no exception and no log; the
work simply stops, after the API has already told the caller it started.

#1479 fixed one instance of this by hand (the background PR watcher in
``services/pr_endgame.py``, which returned ``{"watching": true}`` while the task
doing the reviewing and merging could vanish). This module is that fix's shared
form, so the anchoring set and its ``done_callback`` are written once.

Usage::

    from server.background import spawn

    spawn(watch_for_pr(...))          # instead of asyncio.create_task(...)

The returned task is anchored in a module-level set until it finishes, then
discarded, so the set does not grow without bound.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# The anchor. Every task handed to :func:`spawn` lives here until it is done,
# which is the strong reference the event loop does not hold.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _on_done(task: asyncio.Task[Any]) -> None:
    """Release the anchor and surface a failure the caller never awaited."""
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # Nobody is awaiting this task, so without this the traceback would only
        # appear via asyncio's "exception was never retrieved" warning at GC
        # time — if at all. Log it where it happened instead.
        logger.error(
            "background task %s failed: %s", task.get_name(), exc, exc_info=exc
        )


def spawn(
    coro: Coroutine[Any, Any, _T], *, name: str | None = None
) -> asyncio.Task[_T]:
    """Schedule ``coro`` and hold a strong reference until it completes."""
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def pending_count() -> int:
    """Number of anchored tasks still running (used by the regression tests)."""
    return len(_BACKGROUND_TASKS)
