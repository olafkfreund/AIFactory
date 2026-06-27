"""Saving a Claude profile drops the cached token pool (restart-free swap).

The token pool is cached for the process lifetime, so a profile change made via
the portal (Settings -> Claude Profiles) would not reach a warmed pool until a
pod restart. ``save_profiles`` resets the pool so the new token takes effect on
the next build with no restart.
"""

import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services.agent_credential import CredentialMixin  # noqa: E402


class _Host(CredentialMixin):
    def __init__(self):
        self._token_pool = "warmed-pool"
        self._token_pool_build_lock = threading.Lock()


def test_reset_token_pool_clears_cache():
    h = _Host()
    h.reset_token_pool()
    assert h._token_pool is None


def test_save_profiles_resets_pool(tmp_path, monkeypatch):
    from server.routes import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "get_profiles_file", lambda: tmp_path / "claude-profiles.json"
    )
    calls = []

    class _FakeSvc:
        def reset_token_pool(self):
            calls.append(True)

    # save_profiles imports get_agent_service lazily from server.services.agent_service
    import server.services.agent_service as agent_service_mod

    monkeypatch.setattr(agent_service_mod, "get_agent_service", lambda: _FakeSvc())
    settings_mod.save_profiles({"profiles": [], "activeProfileId": None})
    assert calls == [True], "save_profiles must reset the token pool"
