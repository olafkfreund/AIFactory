"""Guard that no credential name joins an env passthrough list unreviewed (#1759, #1713)."""

from __future__ import annotations

import sys
from collections.abc import Collection, Iterable
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "backend"))
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))

from core.auth import _AGENT_ENV_KEEP, is_denied_env_key  # noqa: E402
from core.child_env import _CREDENTIAL_NAME, _STRIP_VARS, GITHUB_KEEP  # noqa: E402
from providers.antigravity import _GEMINI_KEEP  # noqa: E402
from server.services.build_backend import _PASSTHROUGH_BUILD_ENV  # noqa: E402
from server.utils.subprocess_env import RUNNER_KEEP  # noqa: E402

LISTS: dict[str, Collection[str]] = {
    "GITHUB_KEEP": GITHUB_KEEP,
    "RUNNER_KEEP": RUNNER_KEEP,
    "_PASSTHROUGH_BUILD_ENV": _PASSTHROUGH_BUILD_ENV,
    "_AGENT_ENV_KEEP": _AGENT_ENV_KEEP,
    "_GEMINI_KEEP": _GEMINI_KEEP,
}

EXEMPT: dict[str, frozenset[str]] = {
    # gh / git subprocesses authenticating to GitHub (#1680).
    "GITHUB_KEEP": frozenset(
        {
            "GITHUB_TOKEN",
            "GH_TOKEN",
        }
    ),
    # Backend runner subprocesses: GitHub and provider keys (#1680, OLLAMA #1713).
    "RUNNER_KEEP": frozenset(
        {
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "OPENAI_API_KEY",
            "OPENAI_COMPATIBLE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
            "VOYAGE_API_KEY",
            "OLLAMA_API_KEY",
        }
    ),
    # Build Job pods: Claude auth, GitHub, non-Claude provider keys for
    # byo_llm / phase routing (#689), S3 artifact store.
    "_PASSTHROUGH_BUILD_ENV": frozenset(
        {
            "ANTHROPIC_AUTH_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "OPENAI_API_KEY",
            "OPENAI_COMPATIBLE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OLLAMA_API_KEY",
            "S3_ACCESS_KEY",
            "S3_SECRET_KEY",
        }
    ),
    # Agent's own auth, never blanked by get_agent_env_blanks (#363).
    "_AGENT_ENV_KEEP": frozenset(
        {
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
        }
    ),
    # Antigravity CLI: antigravity.py and antigravity_agentic.py (#1692).
    "_GEMINI_KEEP": frozenset(
        {
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    ),
}


def _credential_names(names: Iterable[str]) -> set[str]:
    return {
        n
        for n in names
        if is_denied_env_key(n)
        or _CREDENTIAL_NAME.search(n) is not None
        or n in _STRIP_VARS
    }


@pytest.mark.parametrize("name", sorted(LISTS))
def test_passthrough_credentials_are_exempted(name: str) -> None:
    found = _credential_names(LISTS[name])
    exempt = EXEMPT[name]
    unreviewed = sorted(found - exempt)
    stale = sorted(exempt - found)
    assert found == exempt, (
        f"{name} credential passthrough mismatch: "
        f"unreviewed={unreviewed}, stale={stale}. "
        "Add a reviewed exemption in tests/test_env_passthrough_credentials.py "
        "or remove the stale entry."
    )


def test_exempt_covers_every_list() -> None:
    assert EXEMPT.keys() == LISTS.keys()
