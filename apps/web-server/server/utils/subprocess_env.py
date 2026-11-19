"""
Subprocess environment helper.

AIFactory is OAuth-only by design — the Claude Agent SDK should never see
``ANTHROPIC_API_KEY`` because that would silently bill against the user's
direct API account instead of their Claude Code subscription
(see ``apps/backend/core/auth.py`` for the canonical policy comment).

Every place AIFactory spawns a subprocess that may run a Claude CLI / SDK
call must build its env via ``make_subprocess_env()`` instead of bare
``os.environ.copy()``. The user's interactive PTY shell is the deliberate
exception — that's their own shell, they expect their normal env.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import logging
import os
import sys
import time
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core import child_env as _core  # noqa: E402 — needs sys.path above
from core.git_credentials import (  # noqa: E402
    github_env,
    github_token,
    sweep_github_dirs,
)

GITHUB_KEEP = _core.GITHUB_KEEP
lfs_push_argv = _core.lfs_push_argv
merge_driver_pins = _core.merge_driver_pins
pin_git_config = _core.pin_git_config
lfs_upload = _core.lfs_upload

__all__ = [
    "AGENT_GITHUB_TOKEN_VAR",
    "GITHUB_KEEP",
    "RUNNER_KEEP",
    "agent_github_token",
    "child_env",
    "dotenv_key_blocked",
    "github_env",
    "github_names",
    "github_token",
    "lfs_push_argv",
    "lfs_upload",
    "make_subprocess_env",
    "merge_driver_pins",
    "pin_git_config",
    "project_github_env",
    "runner_github_env",
    "sweep_github_dirs",
]

RUNNER_KEEP: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "VOYAGE_API_KEY",
    "OLLAMA_API_KEY",
)

AGENT_GITHUB_TOKEN_VAR = "AIFACTORY_AGENT_GITHUB_TOKEN"  # noqa: S105
_blank_agent_token_warned = False
_log = logging.getLogger(__name__)

# Scopes that let a token change or delete things an agent never needs (#1973).
ADMIN_SCOPES = frozenset(
    {
        "delete_repo",
        "admin:org",
        "admin:org_hook",
        "admin:repo_hook",
        "admin:public_key",
        "admin:gpg_key",
        "admin:ssh_signing_key",
        "admin:enterprise",
        "site_admin",
    }
)
ADMIN_POLICY_VAR = "AIFACTORY_AGENT_TOKEN_ADMIN_POLICY"
# App and fine-grained tokens carry permissions, not OAuth scopes: no header to read.
_UNSCOPED_PREFIXES = ("ghs_", "github_pat_", "ghu_")
UNKNOWN = frozenset({"?"})
_SCOPE_TTL = 3600.0
_scope_cache: dict[str, tuple[float, frozenset[str] | None]] = {}
_guard_logged: set[str] = set()


def _fetch_scopes(token: str) -> frozenset[str]:
    import httpx  # noqa: PLC0415

    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    try:
        resp = httpx.get(
            f"{api}/user", headers={"Authorization": f"Bearer {token}"}, timeout=5
        )
    except httpx.HTTPError:
        return UNKNOWN
    header = resp.headers.get("x-oauth-scopes")
    if resp.status_code != httpx.codes.OK or header is None:
        return UNKNOWN
    return frozenset(s.strip() for s in header.split(",") if s.strip())


def admin_scopes(token: str) -> frozenset[str] | None:
    """Admin-class scopes of a classic token; ``None`` if unscoped, ``UNKNOWN`` if unreadable."""
    if token.startswith(_UNSCOPED_PREFIXES):
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _scope_cache.get(key)
    if hit and time.monotonic() - hit[0] < _SCOPE_TTL:
        return hit[1]
    scopes = _fetch_scopes(token)
    found = scopes if scopes == UNKNOWN else scopes & ADMIN_SCOPES
    _scope_cache[key] = (time.monotonic(), found)
    return found


def _admin_policy_refuses() -> bool:
    return os.environ.get(ADMIN_POLICY_VAR, "warn").strip().lower() != "warn"


def _token_allowed(token: str) -> bool:
    """False when ``token`` may not go to an agent child (admin-scoped, policy ``refuse``)."""
    found = admin_scopes(token)
    if not found:
        return True
    refuse = _admin_policy_refuses()
    key = hashlib.sha256(token.encode()).hexdigest()[:12]
    if key not in _guard_logged:
        _guard_logged.add(key)
        what = (
            "unreadable scopes" if found == UNKNOWN else f"admin scopes {sorted(found)}"
        )
        _log.log(
            logging.ERROR if refuse else logging.WARNING,
            "GitHub token for agent children has %s (token id %s); %s",
            what,
            key,
            "withheld from children"
            if refuse
            else f"set {ADMIN_POLICY_VAR}=refuse to withhold it",
        )
    return not refuse


def github_names() -> tuple[str, ...]:
    from core.auth import is_denied_env_key  # noqa: PLC0415
    from core.mcp_credentials import _load_operator_config  # noqa: PLC0415

    names = (*GITHUB_KEEP, "GITHUB_PERSONAL_ACCESS_TOKEN")
    github = _load_operator_config().get("github")
    alias = github.get("tokenEnv") if isinstance(github, dict) else None
    alias = alias.strip() if isinstance(alias, str) else ""
    # A denied name (another secret, or the agent variable) is never an alias:
    # it must not ride into a child through this list.
    if not alias or alias in names or is_denied_env_key(alias):
        return names
    return (*names, alias)


def agent_github_token() -> str:
    global _blank_agent_token_warned  # noqa: PLW0603

    token = os.environ.get(AGENT_GITHUB_TOKEN_VAR, "").strip()
    if (
        AGENT_GITHUB_TOKEN_VAR in os.environ
        and not token
        and not _blank_agent_token_warned
    ):
        _log.warning(
            "Agent GitHub token is blank; runner children fall back to the "
            "control plane's GitHub token"
        )
        _blank_agent_token_warned = True
    return token


def runner_github_env(project_path: str | Path | None = None) -> dict[str, str]:
    # By name: a static import closes the project_registry -> project_store cycle.
    project_registry = importlib.import_module("server.project_registry")
    from server.services import github_app  # noqa: PLC0415

    if github_app.multi_installation():
        token = github_app.token_for_org(
            project_registry.org_id_for_path(project_path), min_ttl=3300
        )
        return (
            dict.fromkeys((*GITHUB_KEEP, "GITHUB_PERSONAL_ACCESS_TOKEN"), token)
            if token
            else {}
        )
    token = agent_github_token()
    if token:
        if not _token_allowed(token):
            return {}
        # The alias too: the runners load_dotenv() themselves, and a value
        # there for an absent alias would win in the MCP probe.
        return dict.fromkeys(github_names(), token)
    env = {name: os.environ[name] for name in github_names() if os.environ.get(name)}
    return {} if any(not _token_allowed(v) for v in set(env.values())) else env


@contextlib.contextmanager
def project_github_env(
    path: str | Path | None, base_env: dict[str, str]
) -> Iterator[dict[str, str]]:
    """``github_env`` with the GitHub token of the org that owns ``path`` (#1700)."""
    from server.services import github_app  # noqa: PLC0415 - avoid import cycle

    with github_app.project_github_token(path), github_env(base_env) as env:
        yield env


def dotenv_key_blocked(key: str) -> bool:
    return key == AGENT_GITHUB_TOKEN_VAR or bool(
        (agent_github_token() or _admin_policy_refuses()) and key in github_names()
    )


def child_env(
    keep: Iterable[str] = (),
    extra: Mapping[str, str] | None = None,
    *,
    runner: bool = False,
) -> dict[str, str]:
    """Core ``child_env`` plus ``TRACEPARENT`` when a span is active (#1680)."""
    env: dict[str, str] = _core.child_env(
        keep=keep, extra={**_traceparent(), **(extra or {})}, runner=runner
    )
    return env


def make_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    strip_anthropic_api_key: bool = True,
    github: bool = False,
    project_path: str | Path | None = None,
) -> dict[str, str]:
    """Return a scrubbed env for LLM runner subprocesses.

    Keeps ``RUNNER_KEEP`` provider keys and the SDK auth path
    (``CLAUDE_CODE_OAUTH_TOKEN``, via the non-denied default). The Anthropic
    direct-API key is stripped unless ``strip_anthropic_api_key=False``, which
    only an explicitly consented batch invocation may pass.
    """
    keep = (
        RUNNER_KEEP
        if strip_anthropic_api_key
        else (*RUNNER_KEEP, "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE")
    )
    env = child_env(keep=keep, extra=extra, runner=True)
    for name in github_names():
        env.pop(name, None)
    if github:
        env.update(runner_github_env(project_path))
    return env


def _traceparent() -> dict[str, str]:
    """``TRACEPARENT`` when an OTel span is active; tracing must never crash a spawn."""
    with contextlib.suppress(Exception):
        from ..observability.tracing import get_current_traceparent

        tp = get_current_traceparent()
        if tp:
            return {"TRACEPARENT": tp}
    return {}
