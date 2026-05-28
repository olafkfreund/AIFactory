"""Shared fixtures for tenant isolation tests (Epic #35 #36 PR-2).

Adds ``apps/web-server`` to ``sys.path`` so ``from server.services...``
imports resolve without polluting the rest of the test tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WEB_SERVER = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))
