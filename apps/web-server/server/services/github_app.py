"""Open and merge PRs as a GitHub App instead of a maintainer's PAT (#1671).

``start()`` mints an installation token at boot and writes it to ``GH_TOKEN`` /
``GITHUB_TOKEN`` in ``os.environ``; ``child_env`` keeps both for gh/git, so every
PR path picks it up. No App vars set means nothing changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

import httpx
import yaml
from jose import jwt

logger = logging.getLogger(__name__)

_KEY_VAR = "AIFACTORY_GITHUB_APP_PRIVATE_KEY"
_APP_VARS = (
    "AIFACTORY_GITHUB_APP_ID",
    "AIFACTORY_GITHUB_APP_INSTALLATION_ID",
    _KEY_VAR,
)
_PAT_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")
_OK_DELAY = 1800
_RETRY_DELAY = 60

_app_id: str | None = None
_installation_id: str | None = None
_key: str | None = None


def configured() -> bool:
    return _key is not None


def _hosts_yml_has_token() -> bool:
    cfg = Path(os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh")
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


def _mint() -> str:
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": _app_id}
    token = str(jwt.encode(claims, _key, algorithm="RS256"))
    resp = httpx.post(
        f"https://api.github.com/app/installations/{_installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    minted = str(resp.json().get("token") or "")
    if not minted:
        raise RuntimeError("GitHub returned no installation token")
    return minted


def _publish(token: str) -> None:
    os.environ["GH_TOKEN"] = os.environ["GITHUB_TOKEN"] = token


def start() -> None:
    global _app_id, _installation_id, _key  # noqa: PLW0603 — process-wide token state
    present = [v for v in _APP_VARS if os.environ.get(v)]
    if not present:
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
    if _hosts_yml_has_token():
        raise RuntimeError(
            "gh hosts.yml holds a github.com token; remove it to use the GitHub App"
        )
    _app_id = os.environ["AIFACTORY_GITHUB_APP_ID"]
    _installation_id = os.environ["AIFACTORY_GITHUB_APP_INSTALLATION_ID"]
    _key = os.environ.pop(_KEY_VAR)
    try:
        _publish(_mint())
    except BaseException:
        _key = None  # fail closed: a failed start leaves nothing configured
        raise


# ponytail: process-wide token, 1h TTL, refreshed every 30 min; a build/Job snapshots it
# at spawn and gets >=30 min. Longer packed builds fail their final push quietly
# (core/workspace_fetch.py:91-108). Upgrade: per-call tokens with #1688.
async def refresh_loop(stop: asyncio.Event) -> None:
    delay = _OK_DELAY
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        if stop.is_set():
            break
        try:
            _publish(await asyncio.to_thread(_mint))
            delay = _OK_DELAY
        except Exception:  # keep the current token, retry soon
            logger.exception("GitHub App token refresh failed; keeping current token")
            delay = _RETRY_DELAY
