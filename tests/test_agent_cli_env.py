"""Non-Claude agent CLIs get a scrubbed env, not the runner's (#1692)."""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import pytest
from providers import (
    antigravity,
    antigravity_agentic,
    codex,
    codex_agentic,
    copilot_agentic,
    opencode_agentic,
)

_FOREIGN = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CONTEXT7_API_KEY",
    "OPENROUTER_API_KEY",
)
_CODEX = ("OPENAI_API_KEY", "CODEX_API_KEY")
_GEMINI = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
_SECRETS = (*_FOREIGN, *_CODEX, *_GEMINI, "COPILOT_GITHUB_TOKEN")
_PASS = ("HOME", "PATH", "OPENAI_BASE_URL", "GOOGLE_CLOUD_PROJECT")


class _SpawnedError(Exception):
    pass


async def _via_query(p: object) -> None:
    await p.query("x")  # type: ignore[attr-defined]
    await p.receive_response().__anext__()  # type: ignore[attr-defined]


async def _via_aenter(p: object) -> None:
    await p.__aenter__()  # type: ignore[attr-defined]


@dataclass(frozen=True)
class _Site:
    mod: ModuleType
    make: Callable[[], object]
    drive: Callable[[object], Awaitable[None]]
    own: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    no_flags: tuple[str, ...] = ()


_SITES = {
    "codex": _Site(codex, codex.CodexCLIProvider, _via_query, _CODEX),
    "codex_agentic": _Site(
        codex_agentic, codex_agentic.CodexAgenticProvider, _via_aenter, _CODEX
    ),
    "antigravity": _Site(
        antigravity,
        antigravity.AntigravityCLIProvider,
        _via_query,
        _GEMINI,
        no_flags=("GEMINI_CLI_TRUST_WORKSPACE",),
    ),
    "antigravity_agentic": _Site(
        antigravity_agentic,
        antigravity_agentic.AntigravityAgenticProvider,
        _via_query,
        _GEMINI,
        ("GEMINI_CLI_TRUST_WORKSPACE",),
    ),
    "copilot_agentic": _Site(
        copilot_agentic,
        copilot_agentic.CopilotAgenticProvider,
        _via_query,
        ("COPILOT_GITHUB_TOKEN",),
        ("COPILOT_ALLOW_ALL",),
    ),
    "opencode_openrouter": _Site(
        opencode_agentic,
        lambda: opencode_agentic.OpenCodeAgenticProvider(model="opencode:openrouter/x"),
        _via_query,
        ("OPENROUTER_API_KEY",),
        ("OPENCODE_DISABLE_AUTOUPDATE",),
    ),
    "opencode_anthropic": _Site(
        opencode_agentic,
        lambda: opencode_agentic.OpenCodeAgenticProvider(model="opencode:anthropic/x"),
        _via_query,
        (),
        ("OPENCODE_DISABLE_AUTOUPDATE",),
    ),
}


@pytest.mark.parametrize("name", list(_SITES))
async def test_agent_cli_env(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    site = _SITES[name]
    seen: list[dict[str, str]] = []

    async def _fake(*_a: object, **kw: object) -> NoReturn:
        env = kw.get("env")
        assert isinstance(env, dict), "env= not passed"
        seen.append(env)
        raise _SpawnedError

    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: "/fake/bin")
    monkeypatch.setattr(site.mod.asyncio, "create_subprocess_exec", _fake)
    for var in ("XDG_CACHE_HOME", "GIT_CONFIG_COUNT", "OPENCODE_DEFAULT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    provider = site.make()
    # Set after construction: proves the env is built at spawn time.
    for var in (*_SECRETS, "OPENAI_BASE_URL", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.setenv(var, "fake")
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(_SpawnedError):
        await site.drive(provider)

    env = seen[0]
    for var in _SECRETS:
        if var not in site.own:
            assert var not in env, "foreign credential leaked"
    for var in (*site.own, *site.flags, *_PASS):
        assert var in env, "expected name missing"
    for var in site.no_flags:
        assert var not in env, "unexpected flag"
    hooks = [v for k, v in env.items() if k.startswith("GIT_CONFIG_KEY_")]
    assert "core.hooksPath" in hooks, "hooks-off pin missing"


@pytest.mark.parametrize(
    ("model", "want"),
    [
        ("anthropic/x", ()),
        ("anthropic-vertex/x", ()),
        (
            "google/x",
            ("GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY"),
        ),
        ("x-y/m", ("X_Y_API_KEY",)),
        ("openrouter/x", ("OPENROUTER_API_KEY",)),
        ("", ()),
        ("gpt-4o", ()),
        ("/m", ()),
    ],
)
def test_opencode_keep(model: str, want: tuple[str, ...]) -> None:
    assert opencode_agentic._opencode_keep(model) == want
