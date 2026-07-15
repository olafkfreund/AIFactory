"""Alembic's env.py must not destroy the app's logging (#844).

MIGRATIONS_AUTO_APPLY=true runs `alembic upgrade head` INSIDE the long-running
web-server, which imports env.py. fileConfig(disable_existing_loggers=False)
preserves existing logger OBJECTS but still rewrites the ROOT logger from
alembic.ini's [logger_root] (level=WARN, handlers=console). Since server.*
loggers propagate to root, the app lost both file handlers and every INFO the
moment it booted, and ran blind thereafter.

Measured in the running pod before the fix:

    after app setup_logging : level=INFO     handlers=[Stream, RotatingFile, RotatingFile]
    after alembic fileConfig: level=WARNING  handlers=[Stream]
    logger.info visible now? False

That is how the RFC-0011 intake poller looked like it "never started" — its
enabled/disabled log line could not be emitted at all.
"""

from __future__ import annotations

import configparser
import logging
from logging.config import fileConfig
from pathlib import Path

_ALEMBIC_INI = Path(__file__).parent.parent / "apps" / "web-server" / "alembic.ini"

# The guard env.py uses: only configure logging when nothing else has.
_GUARD = "not logging.getLogger().handlers"


def test_env_py_guards_fileconfig_on_root_having_no_handlers():
    """The fix itself: env.py must not call fileConfig when the app configured
    logging already. Asserted on the source so the guard cannot be dropped."""
    env_py = (
        Path(__file__).parent.parent
        / "apps"
        / "web-server"
        / "server"
        / "database"
        / "alembic"
        / "env.py"
    ).read_text()
    fileconfig_line = next(
        line
        for line in env_py.splitlines()
        if "fileConfig(config.config_file_name" in line
    )
    guard = next(
        line
        for line in env_py.splitlines()
        if line.startswith("if config.config_file_name is not None")
    )
    assert _GUARD in guard, (
        "env.py must skip fileConfig when root already has handlers, or it "
        f"destroys the app's logging (#844). Guard is: {guard!r}"
    )
    assert "disable_existing_loggers=False" in fileconfig_line


def test_alembic_ini_root_logger_would_clobber_an_app_configured_root():
    """Why the guard is needed, demonstrated against the real alembic.ini.

    This is the pre-fix behaviour: applying alembic.ini to a root logger that the
    app already configured drops it to WARNING and strips its handlers. If this
    ever stops being true the guard may be redundant — but until then it is
    load-bearing.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        # Stand in for the app's setup_logging(): INFO + a non-console handler.
        root.handlers = [logging.NullHandler()]
        root.setLevel(logging.INFO)
        assert root.isEnabledFor(logging.INFO)

        fileConfig(str(_ALEMBIC_INI), disable_existing_loggers=False)

        assert not root.isEnabledFor(logging.INFO), (
            "alembic.ini no longer clobbers an app-configured root logger; "
            "re-check whether env.py still needs its guard (#844)"
        )
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_alembic_ini_still_declares_the_root_logger_the_guard_protects_against():
    """Pin the ini shape the guard exists for, so a silent ini change is visible."""
    cfg = configparser.ConfigParser()
    cfg.read(_ALEMBIC_INI)
    assert cfg.get("logger_root", "level") == "WARN"
    assert cfg.get("logger_root", "handlers") == "console"
