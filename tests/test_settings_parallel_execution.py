#!/usr/bin/env python3
"""
Parallel build execution app settings
=====================================

AIFactory can run a build's independent subtasks concurrently, each agent in its
own git worktree (apps/backend/agents/parallel_runner.py, #376). The /execution
API accepts per-task ``parallel``/``workers`` overrides, but there was no
user-facing way to set an app-level DEFAULT. AppSettings.parallelExecution /
parallelWorkers supply that default.

Covers: default is OFF, both fields round-trip through save/load, and bad worker
counts clamp rather than making settings unloadable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.routes.settings import (  # noqa: E402
    DEFAULT_PARALLEL_WORKERS,
    MAX_PARALLEL_WORKERS,
    MIN_PARALLEL_WORKERS,
    AppSettings,
    SettingsUpdate,
    load_app_settings,
    save_app_settings,
)


@pytest.fixture
def settings_dir(monkeypatch, tmp_path):
    """Point PROJECTS_DATA_DIR at a tmp dir so save/load hits real disk.

    get_settings() returns a module-level singleton built at import time, so the
    attribute is patched directly rather than via env vars.
    """
    from server.config import get_settings

    monkeypatch.setattr(get_settings(), "PROJECTS_DATA_DIR", str(tmp_path))
    # save_app_settings also mirrors soloMode into ~/.aifactory/config.json; keep
    # that out of the real home dir during tests.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_parallel_execution_defaults_off():
    """Parallel execution is opt-in: OFF, with 3 workers, until proven live."""
    settings = AppSettings()

    assert settings.parallelExecution is False
    assert settings.parallelWorkers == DEFAULT_PARALLEL_WORKERS


def test_parallel_settings_round_trip(settings_dir):
    """Both fields survive a save -> load cycle."""
    save_app_settings(AppSettings(parallelExecution=True, parallelWorkers=5))

    loaded = load_app_settings()

    assert loaded.parallelExecution is True
    assert loaded.parallelWorkers == 5


def test_parallel_settings_persist_via_partial_update(settings_dir):
    """A partial SettingsUpdate carries the fields (the PUT route's path)."""
    update = SettingsUpdate(parallelExecution=True, parallelWorkers=4)
    update_dict = update.model_dump(exclude_unset=True)

    assert update_dict == {"parallelExecution": True, "parallelWorkers": 4}

    current = AppSettings().model_dump()
    current.update(update_dict)
    save_app_settings(AppSettings(**current))

    assert load_app_settings().parallelWorkers == 4


def test_snake_case_alias_accepted():
    """Back-compat: snake_case aliases populate the camelCase fields."""
    settings = AppSettings(parallel_execution=True, parallel_workers=6)

    assert settings.parallelExecution is True
    assert settings.parallelWorkers == 6


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, MIN_PARALLEL_WORKERS),  # below floor
        (-5, MIN_PARALLEL_WORKERS),
        (99, MAX_PARALLEL_WORKERS),  # above ceiling
        (None, DEFAULT_PARALLEL_WORKERS),  # missing
        ("not-a-number", DEFAULT_PARALLEL_WORKERS),  # junk
        ("4", 4),  # numeric string coerces
        (MIN_PARALLEL_WORKERS, MIN_PARALLEL_WORKERS),  # bounds are inclusive
        (MAX_PARALLEL_WORKERS, MAX_PARALLEL_WORKERS),
    ],
)
def test_parallel_workers_clamped(raw, expected):
    """Out-of-range/junk worker counts clamp instead of raising.

    A settings.json written by an older build must never become unloadable, so
    this mirrors the uiScale validator's clamp-don't-reject posture.
    """
    assert AppSettings(parallelWorkers=raw).parallelWorkers == expected


def test_bad_worker_count_on_disk_still_loads(settings_dir):
    """A junk value already on disk clamps on load rather than blowing up."""
    (settings_dir / "settings.json").write_text(
        '{"parallelExecution": true, "parallelWorkers": 500}'
    )

    loaded = load_app_settings()

    assert loaded.parallelExecution is True
    assert loaded.parallelWorkers == MAX_PARALLEL_WORKERS


def test_default_matches_coder_constant():
    """Pin the settings default to the executor's real default.

    settings.py cannot import agents.coder at module scope, so the constant is
    duplicated there. This asserts the two never drift apart.
    """
    backend = Path(__file__).parent.parent / "apps" / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from agents.coder import DEFAULT_PARALLEL_WORKERS as CODER_DEFAULT

    assert DEFAULT_PARALLEL_WORKERS == CODER_DEFAULT
