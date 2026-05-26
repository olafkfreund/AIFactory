"""rmux integration for AIFactory (Epic #44).

v1 scope: F1 (Live Agent Console) + F7 (Playwright E2E).  rmux is bundled as
an opt-in binary (gate: ``AIFACTORY_RMUX_ENABLED=true``); the bank-pilot
image ships without it.

This package contains:

- ``wrapper`` — thin async subprocess wrapper around the ``rmux`` CLI
- ``session`` — per-task lifecycle (added in R1)
- ``bridge`` — FIFO ↔ WebSocket transport (added in R1)

See ``guides/plans/2026-05-24-aifactory-rmux-integration-design.md``.
"""

from .wrapper import (
    RmuxDaemonError,
    RmuxError,
    RmuxNotInstalledError,
    RmuxSessionError,
    RmuxWrapper,
)

__all__ = [
    "RmuxWrapper",
    "RmuxError",
    "RmuxDaemonError",
    "RmuxSessionError",
    "RmuxNotInstalledError",
]
