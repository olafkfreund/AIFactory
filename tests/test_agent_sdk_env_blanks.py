"""The Claude CLI and its tools must not inherit the GitHub token (#1723, D1)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
sys.path.insert(0, str(_BACKEND))

TOKEN = "ghp_1723SentinelTokenDoNotLeak"  # gitleaks:allow


class _Stop(Exception):
    pass


def _recorder(seen: list[dict[str, Any]], *, stop: bool = False):
    def _opts(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        if stop:
            raise _Stop
        return kwargs

    return _opts


def _assert_blanked(seen: list[dict[str, Any]]) -> None:
    assert seen, "ClaudeAgentOptions was never built"
    env = seen[0]["env"]
    assert env["GITHUB_TOKEN"] == ""
    assert env["GH_TOKEN"] == ""


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GH_TOKEN", TOKEN)


def test_ai_analyzer_client_blanks_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from runners.ai_analyzer import claude_client as cc

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(cc, "ClaudeAgentOptions", _recorder(seen))
    monkeypatch.setattr(cc, "ClaudeSDKClient", lambda **_kw: object())
    monkeypatch.setattr(cc, "wrap_client_outbound_scrub", lambda c: c)
    monkeypatch.setattr(
        cc.ClaudeAnalysisClient, "_validate_oauth_token", lambda _s: None
    )
    cc.ClaudeAnalysisClient(tmp_path)._create_client(tmp_path / "settings.json")
    _assert_blanked(seen)


def test_insights_runner_blanks_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    from runners import insights_runner as ir

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(ir, "SDK_AVAILABLE", True)
    monkeypatch.setattr(ir, "get_auth_token", lambda: "t")
    monkeypatch.setattr(ir, "ensure_claude_code_oauth_token", lambda: None)
    monkeypatch.setattr(ir, "build_system_prompt", lambda _p: "")
    monkeypatch.setattr(ir, "resolve_model_id", lambda m: m)
    monkeypatch.setattr(ir, "ClaudeAgentOptions", _recorder(seen, stop=True))
    try:
        asyncio.run(
            ir.run_with_sdk(str(tmp_path), "hi", [{"role": "user", "content": "hi"}])
        )
    except _Stop:
        pass  # the runner may also swallow it in its own error handling
    _assert_blanked(seen)


def test_followup_reviewer_passes_blanked_env() -> None:
    # AST fallback (plan step 7f): _run_ai_review needs a full review context,
    # prompt manager and config, far more setup than the other two sites.
    path = _BACKEND / "runners" / "github" / "services" / "followup_reviewer.py"
    calls = [
        n
        for n in ast.walk(ast.parse(path.read_text()))
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "ClaudeAgentOptions"
    ]
    assert calls
    for call in calls:
        env = next((k.value for k in call.keywords if k.arg == "env"), None)
        assert isinstance(env, ast.Call)
        assert getattr(env.func, "id", "") == "get_agent_env_blanks"
