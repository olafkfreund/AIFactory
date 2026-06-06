#!/usr/bin/env python3
"""
Regression test for #384
========================

Setting AIFACTORY_COMPLETION_WEBHOOK (the RFC-0001 completion webhook → CFactory
cockpit) used to crash web-server startup: Settings had extra="forbid", so the
non-APP_ key was rejected as an unknown field. Settings now uses extra="ignore",
so cross-cutting env vars meant for other components (read directly by
services/completion.py) no longer take down startup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.config import Settings  # noqa: E402


def test_completion_webhook_env_does_not_crash_settings(monkeypatch, tmp_path):
    """Settings must construct even when AIFACTORY_COMPLETION_* vars are set."""
    monkeypatch.setenv(
        "AIFACTORY_COMPLETION_WEBHOOK", "http://localhost:3111/api/events"
    )
    monkeypatch.setenv("AIFACTORY_COMPLETION_SENTINEL", "true")
    monkeypatch.setenv("AIFACTORY_COMPLETION_WEBHOOK_TIMEOUT", "5")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))

    settings = Settings()  # must not raise extra_forbidden
    assert settings.HOST  # sanity: a normal field still resolves


def test_unknown_non_app_env_is_ignored(monkeypatch, tmp_path):
    """Any cross-cutting non-APP_ var is ignored, not forbidden (#384)."""
    monkeypatch.setenv("SOME_OTHER_CROSS_REPO_VAR", "value")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))

    settings = Settings()
    assert not hasattr(settings, "some_other_cross_repo_var")


def test_app_prefixed_vars_still_apply(monkeypatch, tmp_path):
    """The APP_ prefix is still honored for real settings."""
    monkeypatch.setenv("APP_MAX_CONCURRENT_TASKS", "9")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))

    settings = Settings()
    assert settings.MAX_CONCURRENT_TASKS == 9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
