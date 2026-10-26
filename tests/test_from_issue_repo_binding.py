"""#1900: a from-issue declared repo must be the project's own repository.

The project's git credential is used against the declared repo, so a member of
project A must not be able to name a repo that A does not own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes import from_issue  # noqa: E402
from server.routes.from_issue import FromIssueRequest, create_from_issue  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Project "p" whose checkout's origin is Owner/Repo; records fetches."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:Owner/Repo.git",
        ],
        check=True,
    )
    registry = {"p": {"path": str(tmp_path), "settings": {}}}
    monkeypatch.setattr("server.project_registry.load_projects", lambda: registry)

    fetched: list = []

    async def _fetch(_pid, number, *, repo_ref=None):
        fetched.append(repo_ref)
        return {"number": number, "title": "Issue", "body": "", "labels": []}

    async def _no_build(**_kwargs):
        pass

    monkeypatch.setattr(from_issue, "_fetch_issue_via_provider", _fetch)
    monkeypatch.setattr(from_issue, "_start_build", _no_build)
    return tmp_path, registry["p"], fetched


def _specs(tmp_path: Path) -> list[Path]:
    specs = tmp_path / ".aifactory" / "specs"
    return [d for d in specs.iterdir() if d.is_dir()] if specs.is_dir() else []


@pytest.mark.parametrize(
    "repo", ["Owner/Repo", "owner/repo", "github:owner/repo", "owner/repo.git"]
)
async def test_matching_repo_is_accepted(project, repo):
    tmp_path, _, fetched = project
    res = await create_from_issue(
        FromIssueRequest(project_id="p", repo=repo, issue_number=7)
    )
    assert res["success"] is True
    assert len(fetched) == 1  # reached the provider fetch
    assert len(_specs(tmp_path)) == 1


@pytest.mark.parametrize(
    ("settings", "cloned_from"),
    [
        ({"gitRepo": "team/svc"}, None),
        ({}, "https://github.com/team/svc.git"),
        ({}, "git@github.com:team/svc.git"),
    ],
)
async def test_configured_repo_and_clone_url_count_as_own(
    project, settings, cloned_from
):
    tmp_path, entry, _ = project
    entry["settings"] = settings
    if cloned_from:
        entry["clonedFrom"] = cloned_from
    res = await create_from_issue(
        FromIssueRequest(project_id="p", repo="team/svc", issue_number=7)
    )
    assert res["success"] is True


@pytest.mark.parametrize(
    "repo", ["victim/private-repo", "owner/repo-other", "gitlab:owner/repo"]
)
async def test_foreign_repo_is_400_before_any_fetch_or_write(project, repo):
    tmp_path, entry, fetched = project
    entry["settings"] = {"gitProvider": "github"}  # makes gitlab: a foreign host
    with pytest.raises(HTTPException) as exc:
        await create_from_issue(
            FromIssueRequest(project_id="p", repo=repo, issue_number=7)
        )
    assert exc.value.status_code == 400
    assert fetched == []
    assert _specs(tmp_path) == []


async def test_payload_path_is_bound_too(project):
    tmp_path, _, _ = project
    with pytest.raises(HTTPException) as exc:
        await create_from_issue(
            FromIssueRequest(
                project_id="p", repo="victim/repo", payload={"number": 1, "title": "x"}
            )
        )
    assert exc.value.status_code == 400
    assert _specs(tmp_path) == []


async def test_no_repo_falls_back_to_the_project(project):
    tmp_path, _, fetched = project
    res = await create_from_issue(FromIssueRequest(project_id="p", issue_number=7))
    assert res["success"] is True
    # No declaration: _get_project_provider uses the project's own repo.
    assert fetched == [""]
    assert len(_specs(tmp_path)) == 1


async def test_project_with_no_known_repo_rejects_a_declaration(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "server.project_registry.load_projects",
        lambda: {"p": {"path": str(tmp_path), "settings": {}}},
    )
    with pytest.raises(HTTPException) as exc:
        await create_from_issue(
            FromIssueRequest(project_id="p", repo="owner/repo", issue_number=7)
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Owner/Repo", "owner/repo"),
        ("https://github.com/Owner/Repo.git", "owner/repo"),
        ("ssh://git@github.com/owner/repo", "owner/repo"),
        ("git@gitlab.com:group/sub/project.git", "group/sub/project"),
        ("https://dev.azure.com/org/proj/_git/repo", "org/proj/repo"),
        ("", ""),
    ],
)
def test_repo_path_normalises_url_forms(value, expected):
    assert from_issue._repo_path(value) == expected
