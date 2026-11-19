"""Open and merge PRs as a GitHub App instead of a maintainer's PAT (#1671).

``start()`` mints an installation token at boot. With one installation it
publishes the token to ``GH_TOKEN`` / ``GITHUB_TOKEN``; with an org map it keeps
tokens per installation and publishes none. No App vars set means no change.
Web login is published through the same ``_publish()`` (``load_web_login``).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from core.git_credentials import has_github_token_provider, use_github_token
from jose import JOSEError, jwt

from server.utils.subprocess_env import AGENT_GITHUB_TOKEN_VAR, child_env, github_token

logger = logging.getLogger(__name__)

_KEY_VAR = "AIFACTORY_GITHUB_APP_PRIVATE_KEY"
_ORG_MAP_VAR = "AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS"
_APP_VARS = (
    "AIFACTORY_GITHUB_APP_ID",
    "AIFACTORY_GITHUB_APP_INSTALLATION_ID",
    _KEY_VAR,
)
_PAT_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")
_OK_DELAY = 1800
_READ_TTL = 60  # a token read must have at least this long to live
_RETRY_DELAY = 60

_app_id: str | None = None
_installation_id: str | None = None
_key: str | None = None
_org_installations: dict[str, str] = {}
_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()
#: Set inside a request whose org the access check settled, as ``(org_id,)``.
#: Path-based wiring may only keep it: a path of another org gets no token.
_pinned: ContextVar[tuple[str | None] | None] = ContextVar(
    "github_app_pinned", default=None
)
#: What a mint can raise: HTTP, a bad id or empty reply, bad or odd JSON, JWT signing.
MINT_ERRORS = (
    httpx.HTTPError,
    RuntimeError,
    ValueError,
    AttributeError,
    TypeError,
    JOSEError,
)


def _valid_installation_id(value: str) -> bool:
    return value.isascii() and value.isdecimal()


def _parse_org_map(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    result: dict[str, str] = {}
    for position, item in enumerate(raw.split(","), 1):
        if not item.strip():
            continue
        if "=" not in item:
            raise RuntimeError(
                f"{_ORG_MAP_VAR} entry {position} needs org=installation"
            )
        org, installation = (part.strip() for part in item.split("=", 1))
        if not org or org in result:
            raise RuntimeError(
                f"{_ORG_MAP_VAR} has invalid or duplicate org id {org!r}"
            )
        if not _valid_installation_id(installation):
            raise RuntimeError(
                f"{_ORG_MAP_VAR} has an invalid installation id for org {org!r}"
            )
        result[org] = installation
    if not result:
        raise RuntimeError(f"{_ORG_MAP_VAR} has no entries")
    return result


def multi_installation() -> bool:
    return bool(_org_installations)


def installation_for_org(org_id: str | None) -> str | None:
    return _org_installations.get(org_id) if org_id is not None else None


def token_for_org(org_id: str | None, *, min_ttl: int = 900) -> str | None:
    installation = installation_for_org(org_id)
    return installation_token(installation, min_ttl=min_ttl) if installation else None


@contextlib.contextmanager
def org_github_token(org_id: str | None, *, pinned: bool = False) -> Iterator[None]:
    if not multi_installation():
        yield
        return
    installation = installation_for_org(org_id)

    def provider() -> str:
        if not installation:
            return ""
        # Callers warm the cache in a thread first. On the event loop thread a
        # cold cache means no token rather than a mint that blocks the loop for
        # up to the 30 s timeout; a worker thread may mint.
        cached = _tokens.get(installation)
        if cached and cached[1] - time.time() >= _READ_TTL:
            return cached[0]
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return ""
        try:
            return installation_token(installation, min_ttl=_READ_TTL)
        except MINT_ERRORS:  # fail closed: no token, gh/git report the auth error
            logger.warning(
                "GitHub App token mint failed for org %s", org_id, exc_info=True
            )
            return ""

    pin = _pinned.set((org_id,)) if pinned else None
    try:
        with use_github_token(provider):
            yield
    finally:
        if pin is not None:
            _pinned.reset(pin)


@contextlib.contextmanager
def project_github_token(path: str | Path | None) -> Iterator[None]:
    """``org_github_token`` for the org that owns ``path``.

    No path keeps an outer block's provider; a path with no owner never does.
    A request's pinned org (see ``org_github_token``) is never replaced: a path
    of another org, or of none, gets no token inside it.
    """
    if not multi_installation() or (not path and has_github_token_provider()):
        yield
        return
    # By name: a static import closes the project_registry -> project_store cycle.
    org = importlib.import_module("server.project_registry").org_id_for_path(path)
    pinned = _pinned.get()
    if pinned is not None and org == pinned[0]:
        yield
        return
    with org_github_token(org if pinned is None else None, pinned=pinned is not None):
        yield


async def warm_org_token(org_id: str | None) -> None:
    """Fill the token cache off the event loop; a mint error is logged, not raised."""
    try:
        await asyncio.to_thread(token_for_org, org_id)
    except MINT_ERRORS:
        logger.warning("GitHub App token mint failed for org %s", org_id, exc_info=True)


def configured() -> bool:
    return _key is not None


def _managed_gh_dir() -> Path:
    """Where web login keeps its gh config, away from ~/.config/gh (#1723)."""
    from server.paths import get_data_dir  # noqa: PLC0415 - paths migrates on import

    d = get_data_dir() / "gh"
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    return d


def _default_gh_dir() -> Path:
    return Path(os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh")


def _hosts_yml_has_token() -> bool:
    return _dir_has_token(_default_gh_dir()) or _dir_has_token(_managed_gh_dir())


def _dir_has_token(cfg: Path) -> bool:
    try:
        data = yaml.safe_load((cfg / "hosts.yml").read_text()) or {}
    except FileNotFoundError:
        return False
    except yaml.YAMLError:
        # The YAML error quotes the bad line, which may hold a token.
        raise RuntimeError("gh hosts.yml is unreadable; fix or remove it") from None
    host = data.get("github.com") if isinstance(data, dict) else None
    if not isinstance(host, dict):
        return False
    users = host.get("users")
    entries = [host, *(users.values() if isinstance(users, dict) else [])]
    return any(isinstance(e, dict) and e.get("oauth_token") for e in entries)


def _mint(installation_id: str) -> tuple[str, float]:
    if not _valid_installation_id(installation_id):
        raise RuntimeError("Invalid GitHub App installation id")
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": _app_id}
    token = str(jwt.encode(claims, _key, algorithm="RS256"))
    resp = httpx.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    minted = str(data.get("token") or "")
    if not minted:
        raise RuntimeError("GitHub returned no installation token")
    try:
        expiry = datetime.fromisoformat(
            data["expires_at"].replace("Z", "+00:00")
        ).timestamp()
    except (KeyError, AttributeError, TypeError, ValueError):
        expiry = time.time() + 3600
    return minted, expiry


def installation_token(installation_id: str, *, min_ttl: int = 900) -> str:
    if not _valid_installation_id(installation_id):
        raise RuntimeError("Invalid GitHub App installation id")
    # Fresh tokens are read without the lock, so one org's mint (HTTP under
    # the lock) never stalls another org's read.
    cached = _tokens.get(installation_id)
    if cached and cached[1] - time.time() >= min_ttl:
        return cached[0]
    with _tokens_lock:
        cached = _tokens.get(installation_id)
        if cached and cached[1] - time.time() >= min_ttl:
            return cached[0]
        minted = _mint(installation_id)
        _tokens[installation_id] = minted
        return minted[0]


def _publish(token: str) -> None:
    os.environ["GH_TOKEN"] = os.environ["GITHUB_TOKEN"] = token


def warn_legacy_web_login() -> None:
    """Warn that an old web-login token sits in the default gh dir (#1723)."""
    old = _default_gh_dir()
    try:
        if old == _managed_gh_dir() or not _dir_has_token(old):
            return
    except (RuntimeError, OSError):  # a warning must never stop startup
        logger.warning("%s/hosts.yml could not be checked for an old token", old)
        return
    logger.warning(
        "%s/hosts.yml holds an old web-login GitHub token. It is still read by "
        "gh calls until you remove it; log in again from the UI to move to "
        "~/.aifactory/gh.",
        old,
    )


def load_web_login() -> None:
    """Publish the token ``gh auth login`` stored in the managed dir, if any."""
    gh = shutil.which("gh")
    if github_token() or configured() or not gh:
        return
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [gh, "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=child_env(extra={"GH_CONFIG_DIR": str(_managed_gh_dir())}),
        )
    except (OSError, subprocess.SubprocessError):
        return
    token = out.stdout.strip()
    if out.returncode == 0 and token:
        _publish(token)
        logger.info("published the web-login GitHub token")


def start() -> None:
    global _app_id, _installation_id, _key, _org_installations  # noqa: PLW0603
    _key = None
    _org_installations = {}
    _tokens.clear()
    raw_map = os.environ.get(_ORG_MAP_VAR, "")
    org_map = _parse_org_map(raw_map)
    present = [v for v in _APP_VARS if os.environ.get(v)]
    if not present:
        if raw_map.strip():
            raise RuntimeError(f"{_ORG_MAP_VAR} needs the GitHub App")
        return
    if len(present) != len(_APP_VARS):
        raise RuntimeError(
            f"GitHub App needs all of {', '.join(_APP_VARS)}; set only {present}"
        )
    from core.mcp_credentials import _load_operator_config  # noqa: PLC0415

    token_env = (_load_operator_config().get("github") or {}).get("tokenEnv")
    for name in (*_PAT_VARS, token_env):
        if name and os.environ.get(name):
            raise RuntimeError(
                f"{name} is set; remove the PAT from the pod to use the GitHub App"
            )
    if not _valid_installation_id(os.environ["AIFACTORY_GITHUB_APP_INSTALLATION_ID"]):
        raise RuntimeError("Invalid AIFACTORY_GITHUB_APP_INSTALLATION_ID")
    if org_map and os.environ.get(AGENT_GITHUB_TOKEN_VAR, "").strip():
        raise RuntimeError(
            f"{_ORG_MAP_VAR} cannot be used with {AGENT_GITHUB_TOKEN_VAR}"
        )
    if _hosts_yml_has_token():
        raise RuntimeError(
            "gh hosts.yml holds a github.com token; remove it to use the GitHub App"
        )
    _app_id = os.environ["AIFACTORY_GITHUB_APP_ID"]
    _installation_id = os.environ["AIFACTORY_GITHUB_APP_INSTALLATION_ID"]
    _key = os.environ.pop(_KEY_VAR)
    try:
        _org_installations = org_map
        if org_map:
            for installation in set(org_map.values()):
                installation_token(installation)
        else:
            _publish(_mint(_installation_id)[0])
    except BaseException:
        _key = None  # fail closed: a failed start leaves nothing configured
        _org_installations = {}
        _tokens.clear()
        raise


# ponytail: process-wide token, 1h TTL, refreshed every 30 min; a build/Job snapshots it
# at spawn and gets >=30 min. Longer packed builds fail their final push quietly
# (core/workspace_fetch.py:91-108). Upgrade: per-call tokens with #1688.
async def refresh_loop(stop: asyncio.Event) -> None:
    if multi_installation():
        return
    delay = _OK_DELAY
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        if stop.is_set():
            break
        try:
            _publish((await asyncio.to_thread(_mint, _installation_id or ""))[0])
            delay = _OK_DELAY
        except Exception:  # keep the current token, retry soon
            logger.exception("GitHub App token refresh failed; keeping current token")
            delay = _RETRY_DELAY
