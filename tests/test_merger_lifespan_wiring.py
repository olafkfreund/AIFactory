"""The merger backstop loop is actually started by main.py (Factory#2586).

``merger_loop`` has its own unit tests, and they would all stay green if
main.py never started it -- a loop written but not wired. This builds the real
production app, enters its lifespan, and requires a tick to happen.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.services import merger


def _app_with_loop(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> tuple[Any, list[int]]:
    for c in list(REGISTRY._collector_to_names.keys()):
        with contextlib.suppress(KeyError):
            REGISTRY.unregister(c)
    monkeypatch.setenv("APP_DISABLE_AUTH", "true")
    if enabled:
        monkeypatch.setenv("AIFACTORY_MERGER_SWEEP", "true")
    else:
        monkeypatch.delenv("AIFACTORY_MERGER_SWEEP", raising=False)

    ticks: list[int] = []

    def tick() -> dict[str, Any]:
        ticks.append(1)
        return {}

    monkeypatch.setattr(merger, "sweep_once", tick)

    # Deferred: importing server.main builds the module-level app (and its
    # Prometheus collectors) at collection time, before the env is set.
    from server.main import create_app  # noqa: PLC0415

    return create_app(), ticks


def test_lifespan_starts_and_stops_the_merger_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, ticks = _app_with_loop(monkeypatch, enabled=True)
    with TestClient(app):
        deadline = time.monotonic() + 5
        while not ticks and time.monotonic() < deadline:
            time.sleep(0.02)
        task = app.state.merger_task
        assert task is not None
        assert ticks, "the loop was created but never ticked"
    assert task.done(), "shutdown must stop the loop"


def test_lifespan_leaves_the_loop_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, ticks = _app_with_loop(monkeypatch, enabled=False)
    with TestClient(app):
        assert app.state.merger_task is None
    assert ticks == []
