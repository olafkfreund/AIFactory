"""
Solo Mode Configuration
=======================

Configuration helpers for AIFactory **solo mode** (issue #276).

Solo mode replaces the full planner -> coder -> QA pipeline with a single
self-directed agent that creates and works its own tasks. It is a
token-saving path for small jobs: one agent writes its own
``implementation_plan.json`` and implements the subtasks in the same loop,
skipping the dedicated planner session, the plan-review gate, and the QA
validation loop.

Resolution order (mirrors ``integrations/bmad/session_config.py`` so the flag
flows through the same channels as other opt-in features):

1. ``AIFACTORY_SOLO_MODE`` environment variable (1/true/yes/on or 0/false/no/off)
2. Per-spec ``task_metadata.json`` ``soloMode`` flag (set by the web UI)
3. Per-spec ``task_metadata.json`` ``skipPlanning`` flag (RFC-0011 intake tier)
4. Global ``~/.aifactory/config.json`` ``solo.enabled``
5. Default: disabled (opt-in, backward compatible)
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _env_solo_mode() -> bool | None:
    """Read the solo-mode env flag.

    Returns:
        True/False when ``AIFACTORY_SOLO_MODE`` is set to a recognised value,
        otherwise None (env flag absent / unrecognised).
    """
    env_value = os.environ.get("AIFACTORY_SOLO_MODE", "").strip().lower()
    if env_value in _TRUTHY:
        return True
    if env_value in _FALSY:
        return False
    return None


def _global_solo_mode() -> bool:
    """Read solo mode from the global ``~/.aifactory/config.json`` file."""
    config_file = Path.home() / ".aifactory" / "config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            return bool(config.get("solo", {}).get("enabled", False))
        except (json.JSONDecodeError, OSError):
            pass
    return False


def is_solo_mode_enabled() -> bool:
    """Check whether solo mode is enabled globally.

    Checks the environment variable first, then the global config file.

    Returns:
        True if solo mode is enabled globally.
    """
    env = _env_solo_mode()
    if env is not None:
        return env
    return _global_solo_mode()


def is_solo_mode_enabled_for_spec(spec_dir: Path) -> bool:
    """Check whether solo mode is enabled for a specific spec.

    The environment variable, when set, wins over everything (operator
    override). Otherwise the per-spec ``task_metadata.json`` ``soloMode`` /
    ``skipPlanning`` flags are honoured, falling back to the global setting.

    Args:
        spec_dir: Path to the spec directory.

    Returns:
        True if solo mode is enabled for this spec.
    """
    # Operator override via environment always wins.
    env = _env_solo_mode()
    if env is not None:
        return env

    # Per-spec override from the web UI / task creation.
    #
    # ``skipPlanning`` is the RFC-0011 intake tier's spelling of the same
    # request: ``factory:low``/``factory:medium`` mean "too small to plan", and
    # skipping the dedicated planner session, the plan-review gate and the QA
    # loop is exactly what solo mode does. The tier path wrote the key into
    # task_metadata.json but nothing in the build ever read it, so a low-tier
    # run planned and stopped there instead of coding (#1078). Reading it HERE
    # rather than at the tier's own call site fixes it for every producer of the
    # key — the intake execution block and PFactory-signed contracts alike.
    #
    # ``soloMode`` is checked first: it is the explicit per-task toggle, so it
    # wins when a task carries both. A present-but-false value of either key is
    # an answer, not an omission, and short-circuits the global setting — the
    # same semantics ``soloMode`` has always had (a ``hard`` tier that says
    # ``skip_planning: false`` must not be silently solo'd by a global default).
    metadata_file = spec_dir / "task_metadata.json"
    if metadata_file.exists():
        try:
            with open(metadata_file) as f:
                metadata = json.load(f)
            for key in ("soloMode", "skipPlanning"):
                if key in metadata:
                    enabled = bool(metadata[key])
                    logger.info(
                        "[Solo Mode] %s — read %s=%s from task_metadata.json",
                        "ENABLED" if enabled else "disabled",
                        key,
                        enabled,
                    )
                    return enabled
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "[Solo Mode] failed to read %s, falling back to global setting",
                metadata_file,
                exc_info=True,
            )

    # Fall back to the global setting.
    return _global_solo_mode()
