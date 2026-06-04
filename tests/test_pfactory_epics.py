"""Tests for PFactory epic child traversal on import (epic #327, #338).

Covers the pure ``extract_child_issue_numbers`` parser and the wired behaviour
in ``import_github_issues``: importing an epic pulls in its children, deduped
and idempotent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pfactory.epics import extract_child_issue_numbers

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))


# ── extract_child_issue_numbers (pure) ─────────────────────────────────────


def test_extracts_checklist_children_in_order():
    body = (
        "## Implementation\n\n"
        "- [ ] #328 — labels\n"
        "- [x] #329 — ingest\n"
        "* [ ] #330 — parse\n"
    )
    assert extract_child_issue_numbers(body) == [328, 329, 330]


def test_ignores_plain_mentions_and_dedupes():
    body = (
        "Relates to #999 and see #888.\n\n"  # plain mentions — not children
        "- [ ] #401 first\n"
        "- [ ] #401 dup\n"  # duplicate collapses
        "- [x] #402 second\n"
    )
    assert extract_child_issue_numbers(body) == [401, 402]


def test_tolerates_non_string_and_empty():
    assert extract_child_issue_numbers(None) == []
    assert extract_child_issue_numbers(123) == []
    assert extract_child_issue_numbers("no children here") == []


# ── import traversal (wired) ───────────────────────────────────────────────

EPIC_BODY = "## Children\n\n- [ ] #101 — child one\n- [ ] #102 — child two\n"

_ISSUES = {
    100: {
        "number": 100,
        "title": "Epic: do big thing",
        "body": EPIC_BODY,
        "state": "open",
        "labels": [
            {"name": "pfactory"},
            {"name": "handoff:aifactory"},
            {"name": "epic"},
        ],
        "url": "https://github.com/x/y/issues/100",
    },
    101: {
        "number": 101,
        "title": "Child one",
        "body": "Do child one.",
        "state": "open",
        "labels": [
            {"name": "pfactory"},
            {"name": "handoff:aifactory"},
            {"name": "priority:p1"},
        ],
        "url": "https://github.com/x/y/issues/101",
    },
    102: {
        "number": 102,
        "title": "Child two",
        "body": "Do child two.",
        "state": "open",
        "labels": [
            {"name": "pfactory"},
            {"name": "handoff:aifactory"},
            {"name": "priority:p2"},
        ],
        "url": "https://github.com/x/y/issues/102",
    },
}


def _fake_gh_by_number():
    def _run(args, cwd=None):
        number = int(args[2])  # ["issue", "view", "<n>", "--json", ...]
        data = _ISSUES.get(number)
        if data is None:
            return {"success": False, "output": ""}
        return {"success": True, "output": json.dumps(data)}

    return _run


def _gh_numbers(project: Path) -> set[int]:
    nums = set()
    for d in (project / ".aifactory" / "specs").iterdir():
        req = d / "requirements.json"
        if req.exists():
            nums.add(json.loads(req.read_text())["githubIssue"]["number"])
    return nums


async def test_importing_epic_pulls_in_children(tmp_path, monkeypatch):
    from server.routes import github

    monkeypatch.setattr(github, "_resolve_project_path", lambda pid: tmp_path)
    monkeypatch.setattr(github, "run_gh_command", _fake_gh_by_number())

    from server.routes.github import ImportIssuesRequest

    result = await github.import_github_issues(
        "proj", ImportIssuesRequest(issueNumbers=[100])
    )

    assert result["data"]["imported"] == 3  # epic + 2 children
    assert _gh_numbers(tmp_path) == {100, 101, 102}


async def test_epic_reimport_is_idempotent(tmp_path, monkeypatch):
    from server.routes import github
    from server.routes.github import ImportIssuesRequest

    monkeypatch.setattr(github, "_resolve_project_path", lambda pid: tmp_path)
    monkeypatch.setattr(github, "run_gh_command", _fake_gh_by_number())

    await github.import_github_issues("proj", ImportIssuesRequest(issueNumbers=[100]))
    # Second import: nothing new created; no duplicate child specs.
    result = await github.import_github_issues(
        "proj", ImportIssuesRequest(issueNumbers=[100])
    )

    assert result["data"]["imported"] == 0
    assert all(s.get("skipped") for s in result["data"]["issues"])
    spec_count = len(
        [d for d in (tmp_path / ".aifactory" / "specs").iterdir() if d.is_dir()]
    )
    assert spec_count == 3  # still exactly epic + 2 children


async def test_non_epic_import_does_not_traverse(tmp_path, monkeypatch):
    from server.routes import github
    from server.routes.github import ImportIssuesRequest

    monkeypatch.setattr(github, "_resolve_project_path", lambda pid: tmp_path)
    monkeypatch.setattr(github, "run_gh_command", _fake_gh_by_number())

    # Import a plain child directly — no epic label, so no traversal.
    result = await github.import_github_issues(
        "proj", ImportIssuesRequest(issueNumbers=[101])
    )
    assert result["data"]["imported"] == 1
    assert _gh_numbers(tmp_path) == {101}
