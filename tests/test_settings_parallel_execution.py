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

import json
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


def test_parallel_execution_defaults_unset():
    """Parallel execution is opt-in and starts UNSET, with 3 workers.

    Tri-state since #905: None means "no opinion", which resolves to OFF once
    the env rung is exhausted, but is deliberately distinct from an explicit
    False -- only an opinion is mirrored to the global config. The UI renders
    None as off (`parallelExecution ?? false`), so this is invisible to users.
    """
    settings = AppSettings()

    assert settings.parallelExecution is None
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


# ── #905: the setting is wired to intake, end to end ───────────────────────


@pytest.fixture
def _no_intake_env(monkeypatch):
    """The env rung sits below the setting; unset it so it cannot mask a bug."""
    monkeypatch.delenv("AIFACTORY_INTAKE_PARALLEL", raising=False)
    monkeypatch.delenv("AIFACTORY_INTAKE_WORKERS", raising=False)


def _intake_task_metadata() -> dict:
    """Run the real intake chain: config.json -> execution block -> task metadata.

    Mirrors from_issue.py for an unlabelled issue (classify_parallel returns
    (None, None)), through the same mapping that writes task_metadata.json.
    """
    backend = Path(__file__).parent.parent / "apps" / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from intake import build_execution_block
    from pfactory.tiers import Tier
    from trusted_plan import execution_profile_to_metadata

    return execution_profile_to_metadata(build_execution_block(Tier.MEDIUM))


def test_portal_toggle_reaches_intake_task_metadata(settings_dir, _no_intake_env):
    """The acceptance criterion for #905: flipping the toggle changes the build.

    Before the fix this asserted nothing useful — intake read only
    AIFACTORY_INTAKE_PARALLEL, so the portal setting was dead UI.
    """
    save_app_settings(AppSettings(parallelExecution=True, parallelWorkers=5))

    meta = _intake_task_metadata()

    assert meta["parallel"] is True
    assert meta["workers"] == 5


def test_portal_toggle_off_keeps_intake_serial(settings_dir, _no_intake_env):
    """The other half of the toggle: saving OFF must produce a serial build."""
    save_app_settings(AppSettings(parallelExecution=False, parallelWorkers=5))

    meta = _intake_task_metadata()

    assert meta["parallel"] is False
    assert "workers" not in meta  # a cap is meaningless to a serial build


def test_unset_toggle_does_not_shadow_the_operator_env_default(
    settings_dir, monkeypatch
):
    """Saving unrelated settings must not silently kill the fleet env default.

    The setting rung outranks env, so if a never-touched parallelExecution
    mirrored as `enabled: false` it would pin every intake build serial the
    moment a user saved a theme change -- and no env var could undo it. None
    means "no opinion": the parallel block is not written at all.
    """
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "1")

    save_app_settings(AppSettings(uiScale=1.2))  # a save that is not about parallelism

    assert "parallel" not in json.loads(
        (settings_dir / ".aifactory" / "config.json").read_text()
    )
    assert _intake_task_metadata()["parallel"] is True  # env default survives


def test_clearing_the_toggle_removes_a_stale_opinion(settings_dir, monkeypatch):
    """Going back to unset must not leave the old opinion overriding env."""
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "1")
    save_app_settings(AppSettings(parallelExecution=False))
    assert _intake_task_metadata()["parallel"] is False  # opinion beats env

    save_app_settings(AppSettings(parallelExecution=None))

    assert _intake_task_metadata()["parallel"] is True  # env rung restored


def test_explicit_serial_label_still_overrides_the_toggle(settings_dir, _no_intake_env):
    """Per-issue intent stays the most specific rung once the setting is live."""
    save_app_settings(AppSettings(parallelExecution=True, parallelWorkers=5))

    backend = Path(__file__).parent.parent / "apps" / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from intake import build_execution_block
    from pfactory.tiers import Tier, classify_parallel

    # A real factory:serial label, resolved the way from_issue.py resolves it.
    parallel, workers = classify_parallel(["factory:serial"])
    block = build_execution_block(Tier.MEDIUM, parallel=parallel, workers=workers)

    assert block["parallel"] is False
