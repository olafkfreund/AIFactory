"""Pytest fixtures for Claude enforcement tests (Epic #35 v1.2 #207).

Mirrors the pattern from tests/llm_gateway/conftest.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
WEB_SERVER_ROOT = REPO_ROOT / "apps" / "web-server"

# Both paths needed: enforcement.py imports from backend; audit hook
# imports from web-server.  Order matters: web-server first so that
# server.services.llm_pii_redactor resolves before the backend path.
for _root in (WEB_SERVER_ROOT, BACKEND_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
