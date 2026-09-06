"""Secrets are written 0600 from creation, never chmod'd after the fact.

Fork drift: PFactory has ``paths.atomic_write_secret_json`` and TFactory has
``paths.write_secret_file``; AIFactory had NEITHER and still did
``write_text`` then ``chmod(0o600)`` on the API token, the JWT signing secret
and the Claude profile stores. That pattern has two REPRODUCED defects:

1. **A world-readable window.** ``write_text`` creates the file at the umask
   default (typically 0644) and only narrows it *after* the secret is on disk.
2. **Truncation in place.** A concurrent ``load_profiles`` reads a torn file,
   swallows the ``JSONDecodeError``, returns ``{"profiles": []}`` — and the next
   save writes THAT back, destroying every profile and its token.

The window tests assert on the mode of the file *at the moment it first holds
the secret*, not on the final mode: a final-mode assertion passes against the
buggy code and proves nothing.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import types
from pathlib import Path

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

import pytest  # noqa: E402
from server.paths import atomic_write_secret_json, write_secret_file  # noqa: E402

SECRET = "sk-ant-oat01-THIS-IS-THE-SECRET"


@pytest.fixture
def permissive_umask():
    """Run with umask 0 so a 0644-creating write is not masked into looking safe."""
    old = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(old)


class _ChmodSpy:
    """Records the mode and content of every file at the instant chmod is called.

    ``write_text`` + ``chmod(0o600)`` shows up here as a sample whose content is
    already the secret while the mode is still the umask default: that IS the
    readable window. A helper that creates the file 0600 from birth never calls
    chmod on a secret at all, so it records nothing.
    """

    def __init__(self) -> None:
        self.windows: list[tuple[str, str]] = []
        self._real = Path.chmod

    def __enter__(self) -> _ChmodSpy:
        spy = self

        def chmod(self_path: Path, mode: int, **kw):  # type: ignore[no-untyped-def]
            try:
                st = self_path.stat()
                content = self_path.read_bytes()
            except OSError:
                return spy._real(self_path, mode, **kw)
            if stat.S_IMODE(st.st_mode) & 0o077:
                spy.windows.append((str(self_path), oct(stat.S_IMODE(st.st_mode))))
                spy.secrets_exposed = content
            return spy._real(self_path, mode, **kw)

        Path.chmod = chmod  # type: ignore[method-assign,assignment]
        return self

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        Path.chmod = self._real  # type: ignore[method-assign]

    def assert_no_window(self) -> None:
        assert not self.windows, (
            "secret file existed at a group/other-readable mode before chmod "
            f"narrowed it (the readable window): {self.windows}"
        )


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


def _mode_during_write(path: Path, text: str) -> int:
    """Capture the mode as seen from INSIDE the write, not after it finishes."""
    seen: dict[str, int] = {}
    real_write = os.write

    def spy(fd: int, data: bytes) -> int:
        seen.setdefault("mode", stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write(fd, data)

    os.write = spy  # type: ignore[assignment]
    try:
        write_secret_file(path, text)
    finally:
        os.write = real_write  # type: ignore[assignment]
    if "mode" not in seen:
        pytest.fail(
            "secret was not written via os.open/os.write with an explicit mode; "
            "a write_text-then-chmod path leaves it world-readable mid-write"
        )
    return seen["mode"]


def test_helper_is_0600_from_creation(tmp_path: Path, permissive_umask: None) -> None:
    p = tmp_path / ".token"
    assert _mode_during_write(p, SECRET) == 0o600
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.read_text() == SECRET


def test_helper_repairs_an_existing_loose_mode(
    tmp_path: Path, permissive_umask: None
) -> None:
    """os.replace swaps the inode, so a file left 0644 by an older build heals."""
    p = tmp_path / ".jwt_secret"
    p.write_text("old")
    p.chmod(0o644)
    write_secret_file(p, SECRET)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_json_helper_is_0600_and_valid(tmp_path: Path, permissive_umask: None) -> None:
    p = tmp_path / "claude-profiles.json"
    atomic_write_secret_json(p, {"profiles": [{"token": SECRET}]})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text())["profiles"][0]["token"] == SECRET


def test_concurrent_reader_never_sees_a_torn_file(tmp_path: Path) -> None:
    """The data-loss path: a torn read makes load_profiles return no profiles.

    write_text truncates in place. os.replace publishes whole-old or whole-new.
    """
    p = tmp_path / "claude-profiles.json"
    payload = {
        "activeProfileId": "p1",
        "profiles": [
            {"id": f"p{i}", "name": f"Account {i}", "token": SECRET * 40}
            for i in range(1, 6)
        ],
    }
    atomic_write_secret_json(p, payload)

    torn: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            atomic_write_secret_json(p, payload)

    def reader() -> None:
        while not stop.is_set():
            try:
                data = json.loads(p.read_text())
            except json.JSONDecodeError as e:
                torn.append(f"JSONDecodeError: {e}")
                return
            except FileNotFoundError:
                torn.append("file vanished mid-write")
                return
            if len(data.get("profiles", [])) != 5:
                torn.append(f"partial read: {len(data.get('profiles', []))} profiles")
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(2)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not torn, f"reader saw a torn secret store (data-loss path): {torn[:3]}"


def test_failed_json_write_leaves_no_temp_droppings(tmp_path: Path) -> None:
    p = tmp_path / "secret.json"

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        atomic_write_secret_json(p, {"bad": Unserialisable()})
    assert not list(tmp_path.iterdir()), list(tmp_path.iterdir())


# ---------------------------------------------------------------------------
# The call sites
# ---------------------------------------------------------------------------


def test_config_token_and_jwt_secret_have_no_readable_window(
    tmp_path: Path, permissive_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.Settings._get_or_generate_{token,jwt_secret} — config.py:289/:322."""
    from server import config as config_mod

    monkeypatch.setattr(config_mod, "get_data_file", lambda name: tmp_path / name)
    settings = config_mod.Settings.__new__(config_mod.Settings)

    with _ChmodSpy() as spy:
        token = config_mod.Settings._get_or_generate_token(settings)
        secret = config_mod.Settings._get_or_generate_jwt_secret(settings)
    spy.assert_no_window()

    for path, value in (
        (tmp_path / ".token", token),
        (tmp_path / ".jwt_secret", secret),
    ):
        assert path.read_text().strip() == value
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_json_store_has_no_readable_window(
    tmp_path: Path, permissive_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """routes.settings._write_json_store — settings.py:524."""
    from server.routes import settings as settings_mod

    monkeypatch.setattr(
        settings_mod,
        "get_settings",
        lambda: types.SimpleNamespace(PROJECTS_DATA_DIR=str(tmp_path)),
    )

    with _ChmodSpy() as spy:
        settings_mod._write_json_store(
            "api-profiles.json", {"profiles": [{"token": SECRET}]}
        )
    spy.assert_no_window()

    store = tmp_path / "api-profiles.json"
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert json.loads(store.read_text())["profiles"]


@pytest.mark.asyncio
async def test_regenerate_api_token_has_no_readable_window(
    tmp_path: Path, permissive_umask: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """routes.settings.regenerate_api_token — settings.py:660."""
    from server import paths as paths_mod
    from server.routes import settings as settings_mod

    monkeypatch.setattr(paths_mod, "get_data_file", lambda name: tmp_path / name)

    with _ChmodSpy() as spy:
        result = await settings_mod.regenerate_api_token()
    spy.assert_no_window()

    token_file = tmp_path / ".token"
    assert token_file.read_text().strip() == result["token"]
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_update_active_profile_has_no_readable_window(
    tmp_path: Path, permissive_umask: None
) -> None:
    """services.agent_credential.CredentialMixin._update_active_profile — :394."""
    from server.services.agent_credential import CredentialMixin

    profiles_file = tmp_path / "claude-profiles.json"
    profiles_file.write_text(
        json.dumps(
            {
                "activeProfileId": "p1",
                "profiles": [
                    {"id": "p1", "name": "A", "oauthToken": SECRET + "-1"},
                    {"id": "p2", "name": "B", "oauthToken": SECRET + "-2"},
                ],
            }
        )
    )

    class _Svc(CredentialMixin):
        settings = types.SimpleNamespace(PROJECTS_DATA_DIR=str(tmp_path))

    with _ChmodSpy() as spy:
        _Svc()._update_active_profile("p2", "B")
    spy.assert_no_window()

    assert json.loads(profiles_file.read_text())["activeProfileId"] == "p2"
    assert stat.S_IMODE(profiles_file.stat().st_mode) == 0o600
