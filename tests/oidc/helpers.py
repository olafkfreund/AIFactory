"""Utilities shared across P3 OIDC acceptance tests."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER_ROOT = REPO_ROOT / "apps" / "web-server"

if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER_ROOT))


# Module names we evict between tests to force a fresh OIDC client
# instance under a different env (issuer URL / client_id / etc.). Same
# pattern as tests/secrets/helpers._MODULES_TO_EVICT.
_MODULES_TO_EVICT = (
    "server.auth.oidc",
    "server.auth.userinfo_cache",
    "server.auth",
    "server.routes.auth",
)


def reimport_oidc(env: dict[str, str]) -> None:
    """Re-import ``server.auth.oidc`` with fresh OIDC env vars.

    Used by tests that need to reconfigure the OIDC client mid-run
    (e.g. swapping presets between Keycloak / Okta / Azure AD).
    """
    import os
    for k, v in env.items():
        os.environ[k] = v
    for m in _MODULES_TO_EVICT:
        sys.modules.pop(m, None)


def authlib_available() -> bool:
    """True iff ``authlib`` is importable.

    OIDC tests skip cleanly when the library isn't installed yet
    (pre-P3.1 state).
    """
    try:
        importlib.import_module("authlib")
        return True
    except ImportError:
        return False
