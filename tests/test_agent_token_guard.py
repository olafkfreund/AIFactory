# ruff: noqa: S105
"""An admin-scoped GitHub token must not reach agent children when refused (#1973)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "backend"))
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))

from server.utils import subprocess_env as se  # noqa: E402

CLASSIC = "ghp_guardSentinelDoNotLeak"  # gitleaks:allow
ADMIN = frozenset({"repo", "delete_repo", "admin:org"})
NARROW = frozenset({"repo"})


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(se.AGENT_GITHUB_TOKEN_VAR, raising=False)
    monkeypatch.delenv(se.ADMIN_POLICY_VAR, raising=False)
    monkeypatch.setattr(se, "_scope_cache", {})
    monkeypatch.setattr(se, "_guard_logged", set())
    monkeypatch.setattr(se, "github_names", lambda: ("GITHUB_TOKEN", "GH_TOKEN"))
    from server.services import github_app  # noqa: PLC0415

    monkeypatch.setattr(github_app, "multi_installation", lambda: False)


def _scopes(monkeypatch: pytest.MonkeyPatch, scopes: frozenset[str]) -> list[str]:
    calls: list[str] = []

    def fetch(token: str) -> frozenset[str]:
        calls.append(token)
        return scopes

    monkeypatch.setattr(se, "_fetch_scopes", fetch)
    return calls


def test_warn_passes_admin_token_and_logs_once_without_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _scopes(monkeypatch, ADMIN)
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    monkeypatch.setenv("GH_TOKEN", CLASSIC)
    with caplog.at_level(logging.WARNING):
        first = se.runner_github_env()
        second = se.runner_github_env()
    assert first == second == {"GITHUB_TOKEN": CLASSIC, "GH_TOKEN": CLASSIC}
    records = [r for r in caplog.records if "admin scopes" in r.getMessage()]
    assert len(records) == 1
    assert CLASSIC not in caplog.text
    assert "delete_repo" in caplog.text


def test_refuse_withholds_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _scopes(monkeypatch, ADMIN)
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, "refuse")
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    monkeypatch.setenv("GH_TOKEN", "ghp_other")
    assert se.runner_github_env() == {}


def test_refuse_passes_token_without_admin_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scopes(monkeypatch, NARROW)
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, "refuse")
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    assert se.runner_github_env() == {"GITHUB_TOKEN": CLASSIC}


@pytest.mark.parametrize(("policy", "passes"), [("refuse", False), ("warn", True)])
def test_unreadable_scopes(
    monkeypatch: pytest.MonkeyPatch, policy: str, passes: bool
) -> None:
    _scopes(monkeypatch, se.UNKNOWN)
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, policy)
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    assert bool(se.runner_github_env()) is passes


@pytest.mark.parametrize("prefix", ["ghs_", "github_pat_", "ghu_"])
def test_unscoped_token_kinds_are_not_fetched(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    calls = _scopes(monkeypatch, ADMIN)
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, "refuse")
    monkeypatch.setenv("GITHUB_TOKEN", f"{prefix}abc")
    assert se.runner_github_env() == {"GITHUB_TOKEN": f"{prefix}abc"}
    assert calls == []


def test_typo_policy_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _scopes(monkeypatch, ADMIN)
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, "wan")
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    assert se.runner_github_env() == {}


def test_scopes_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _scopes(monkeypatch, NARROW)
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    for _ in range(3):
        se.runner_github_env()
    assert len(calls) == 1


def test_agent_token_branch_is_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _scopes(monkeypatch, ADMIN)
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, "refuse")
    monkeypatch.setenv(se.AGENT_GITHUB_TOKEN_VAR, CLASSIC)
    assert se.runner_github_env() == {}


def test_dotenv_cannot_restore_a_refused_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not se.dotenv_key_blocked("GITHUB_TOKEN")
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, "refuse")
    assert se.dotenv_key_blocked("GITHUB_TOKEN")
    assert not se.dotenv_key_blocked("OPENAI_API_KEY")


def test_make_subprocess_env_drops_refused_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scopes(monkeypatch, ADMIN)
    monkeypatch.setenv(se.ADMIN_POLICY_VAR, "refuse")
    monkeypatch.setenv("GITHUB_TOKEN", CLASSIC)
    env = se.make_subprocess_env(github=True)
    assert "GITHUB_TOKEN" not in env
    assert CLASSIC not in env.values()
