"""Agent trailers reach the default branch via the PR squash body (Factory#1960)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.services import approval  # noqa: E402

BLOCK = "Agent-Identity: coder-1\nAutonomy-Tier: medium\nOriginator: alice"


@pytest.fixture
def argv(monkeypatch, tmp_path):
    (tmp_path / "requirements.json").write_text(
        json.dumps(
            {
                "agent_identity": "coder-1",
                "originator": "alice",
                "intake": {"autonomy_tier": "medium"},
            }
        )
    )
    seen: list[list[str]] = []
    monkeypatch.setattr(approval, "_open_pr", lambda *_: (7, "OPEN"))

    def fake_gh(argv: list[str], **_: object) -> dict[str, bool]:
        seen.append(argv)
        return {"success": True}

    monkeypatch.setattr(approval, "run_gh_command", fake_gh)
    return seen


def test_squash_body_carries_trailers_when_on(argv, tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AGENT_COMMIT_TRAILERS", "1")
    assert approval.merge_pull_request(tmp_path, "b", spec_dir=tmp_path)[0]
    assert argv[0][-2:] == ["--body", BLOCK]


def test_argv_unchanged_when_off(argv, tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_AGENT_COMMIT_TRAILERS", raising=False)
    assert approval.merge_pull_request(tmp_path, "b", spec_dir=tmp_path)[0]
    assert argv == [["pr", "merge", "7", "--squash"]]
