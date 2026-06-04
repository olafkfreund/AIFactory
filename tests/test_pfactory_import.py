"""Integration test for PFactory governance persistence at GitHub import
(epic #327, issue #329).

Drives the real ``import_github_issues`` web route with ``run_gh_command`` and
``_resolve_project_path`` mocked, and asserts the written ``requirements.json``:
governed PFactory issues gain the ``governed``/``pfactory`` markers; ordinary
issues are untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The route lives in the web-server package; add both apps to the path.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes import github  # noqa: E402
from server.routes.github import ImportIssuesRequest  # noqa: E402


def _fake_gh(labels: list[str]):
    def _run(args, cwd=None):
        return {
            "success": True,
            "output": json.dumps(
                {
                    "number": 7,
                    "title": "Add status endpoint",
                    "body": "Add GET /status.",
                    "state": "open",
                    "labels": [{"name": name} for name in labels],
                    "url": "https://github.com/x/y/issues/7",
                }
            ),
        }

    return _run


def _written_requirements(tmp_path: Path, result: dict) -> dict:
    spec_id = result["data"]["issues"][0]["specId"]
    req_file = tmp_path / ".aifactory" / "specs" / spec_id / "requirements.json"
    return json.loads(req_file.read_text())


async def test_import_persists_governance_for_pfactory_issue(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "_resolve_project_path", lambda pid: tmp_path)
    monkeypatch.setattr(
        github, "run_gh_command", _fake_gh(["pfactory", "handoff:aifactory", "epic"])
    )

    result = await github.import_github_issues(
        "proj", ImportIssuesRequest(issueNumbers=[7])
    )
    assert result["success"] is True

    req = _written_requirements(tmp_path, result)
    assert req["governed"] is True
    assert req["pfactory"]["governed"] is True
    assert req["pfactory"]["handoff"] == "aifactory"
    assert req["pfactory"]["is_epic"] is True
    assert req["pfactory"]["taxonomy"] == "v1"


async def test_import_pfactory_routed_to_tfactory_is_not_governed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(github, "_resolve_project_path", lambda pid: tmp_path)
    monkeypatch.setattr(
        github, "run_gh_command", _fake_gh(["pfactory", "handoff:tfactory"])
    )

    result = await github.import_github_issues(
        "proj", ImportIssuesRequest(issueNumbers=[7])
    )
    req = _written_requirements(tmp_path, result)
    # Marker present, but routed away from AIFactory → not governed here.
    assert req["governed"] is False
    assert req["pfactory"]["handoff"] == "tfactory"


async def test_import_non_pfactory_issue_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "_resolve_project_path", lambda pid: tmp_path)
    monkeypatch.setattr(github, "run_gh_command", _fake_gh(["bug", "backend"]))

    result = await github.import_github_issues(
        "proj", ImportIssuesRequest(issueNumbers=[7])
    )
    req = _written_requirements(tmp_path, result)
    assert "governed" not in req
    assert "pfactory" not in req
