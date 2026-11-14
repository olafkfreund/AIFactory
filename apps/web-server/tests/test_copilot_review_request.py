"""Copilot request confirmation and ruleset reviewer behavior."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
_BACKEND = _WEB_SERVER.parent / "backend"
for _path in (str(_WEB_SERVER), str(_BACKEND)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from server.services import pr_endgame as pe  # noqa: E402


@pytest.mark.parametrize(
    ("result", "confirmed"),
    [
        (pe.CmdResult(0, '{"requested_reviewers": [{"login": "Copilot"}]}', ""), True),
        (
            pe.CmdResult(
                0,
                '{"requested_reviewers": [{"login": "copilot-pull-request-reviewer[bot]"}]}',
                "",
            ),
            True,
        ),
        (pe.CmdResult(1, '{"requested_reviewers": [{"login": "Copilot"}]}', ""), False),
        (pe.CmdResult(0, '{"requested_reviewers": []}', ""), False),
        (
            pe.CmdResult(
                1, "", "gh: Resource not accessible by integration (HTTP 403)"
            ),
            False,
        ),
        *[
            (pe.CmdResult(0, output, ""), False)
            for output in (
                "not json",
                "[]",
                "null",
                "{}",
                '{"requested_reviewers": 123}',
                '{"requested_reviewers": [null]}',
                '{"requested_reviewers": ["copilot"]}',
                '{"requested_reviewers": [{"login": 123}]}',
            )
        ],
    ],
)
def test_request_requires_success_and_confirmed_login(result, confirmed, caplog):
    def runner(_argv, _cwd):
        return result

    with caplog.at_level(logging.INFO, logger=pe.logger.name):
        assert (
            pe.request_copilot_review("owner", "repo", 42, runner=runner) is confirmed
        )
    records = [record for record in caplog.records if record.name == pe.logger.name]
    assert sum(record.levelno == logging.WARNING for record in records) == (
        not confirmed
    )
    assert not any(record.levelno == logging.INFO for record in records)


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("gh", 1), OSError("boom")])
def test_runner_failure_is_warning_not_exception(error, caplog):
    def runner(_argv, _cwd):
        raise error

    with caplog.at_level(logging.INFO, logger=pe.logger.name):
        assert pe.request_copilot_review("owner", "repo", 42, runner=runner) is False
    records = [record for record in caplog.records if record.name == pe.logger.name]
    assert sum(record.levelno == logging.WARNING for record in records) == 1
    assert not any(record.levelno == logging.INFO for record in records)


@pytest.mark.parametrize(
    ("outcome", "reason", "owner", "detail"),
    [
        (pe.CmdResult(1, "SENTINEL", "a\nb"), "gh failed", "owner", "a\\nb"),
        (
            pe.CmdResult(0, '{"requested_reviewers": [], "x": "SENTINEL"}', ""),
            "Copilot was not listed",
            "owner\nforged",
            "",
        ),
        (pe.CmdResult(0, "SENTINEL", ""), "Copilot was not listed", "owner", ""),
        (OSError("SENTINEL"), "gh could not be run", "owner", "OSError"),
    ],
)
def test_warning_has_remedy_and_safe_details(outcome, reason, owner, detail, caplog):
    def runner(_argv, _cwd):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with caplog.at_level(logging.INFO, logger=pe.logger.name):
        assert pe.request_copilot_review(owner, "repo", 42, runner=runner) is False
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == pe.logger.name and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    warning = warnings[0]
    safe_owner = owner.replace("\n", "\\n")
    assert f"{safe_owner}/repo" in warning
    assert all(
        part in warning
        for part in (
            "owner",
            "repo",
            "42",
            reason,
            "copilot-ruleset",
            "stop for a human",
            "docs/docs/concepts/github-app.md",
        )
    )
    assert detail in warning
    assert "App token" not in warning
    assert "SENTINEL" not in warning
    assert "\n" not in warning


def test_resolve_ruleset_reviewer(monkeypatch):
    monkeypatch.setenv("AIFACTORY_PR_REVIEWER", "copilot-ruleset")
    assert pe.resolve_pr_reviewer() == "copilot-ruleset"


@pytest.mark.parametrize("reviewer", ["copilot", "copilot-ruleset"])
async def test_endgame_waits_for_copilot_even_if_request_unconfirmed(
    reviewer, tmp_path, monkeypatch
):
    requested = []
    opened = []
    watched = []

    def request(owner, repo, pr, *, runner):
        requested.append((owner, repo, pr, runner))
        return False

    async def watch(**kwargs):
        watched.append(kwargs)
        return {"merged": False, "reason": "stub"}

    monkeypatch.setattr(pe, "create_pr", lambda **_kwargs: 42)
    monkeypatch.setattr(pe, "_pr_title_body", lambda *_args: ("title", "body"))
    monkeypatch.setattr(pe, "request_copilot_review", request)
    monkeypatch.setattr(pe, "watch_and_finish", watch)

    result = await pe.run_pr_endgame(
        spec_dir=tmp_path,
        spec_id="001-x",
        trusted=None,
        worktree=tmp_path,
        branch="feature",
        base="main",
        repo="owner/repo",
        reviewer=reviewer,
        on_pr_opened=opened.append,
        background=False,
    )

    assert result["ok"] is True
    assert len(requested) == (reviewer == "copilot")
    assert not opened
    assert len(watched) == 1
    assert watched[0]["require_copilot"] is True
