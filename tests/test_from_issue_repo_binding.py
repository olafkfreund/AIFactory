"""#1900: a from-issue declared repo must be the project's own repository.

The project's git credential is used against the declared repo, so a member of
project A must not be able to name a repo that A does not own: not another
path, not the same path on another host, not another provider.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes import from_issue  # noqa: E402
from server.routes.from_issue import FromIssueRequest, create_from_issue  # noqa: E402


@pytest.fixture
def fetched(monkeypatch):
    """Stub the network edges; record what reached the provider fetch."""
    calls: list = []

    async def _fetch(_pid, number, *, repo_ref=None):
        calls.append(repo_ref)
        return {"number": number, "title": "Issue", "body": "", "labels": []}

    async def _no_build(**_kwargs):
        pass

    monkeypatch.setattr(from_issue, "_fetch_issue_via_provider", _fetch)
    monkeypatch.setattr(from_issue, "_start_build", _no_build)
    return calls


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Project "p" at tmp_path; tests fill in its settings / clonedFrom."""
    entry: dict = {"path": str(tmp_path), "settings": {}}
    monkeypatch.setattr("server.project_registry.load_projects", lambda: {"p": entry})
    return entry


@pytest.fixture
def github_checkout(tmp_path, registry):
    """Project "p" is a checkout whose origin is github.com Owner/Repo."""
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
    return registry


def _specs(tmp_path: Path) -> list[Path]:
    specs = tmp_path / ".aifactory" / "specs"
    return [d for d in specs.iterdir() if d.is_dir()] if specs.is_dir() else []


# Called as over HTTP: the #1898 membership check runs first. The legacy
# service token passes it without a DB query, so the db is a placeholder.
_SERVICE = SimpleNamespace(
    state=SimpleNamespace(user={"id": "default", "role": "user", "is_service": True})
)


async def _call(req: FromIssueRequest):
    return await create_from_issue(req, _SERVICE, db=SimpleNamespace())


async def _create(repo: str | None):
    return await _call(FromIssueRequest(project_id="p", repo=repo, issue_number=7))


async def _rejected(repo: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await _create(repo)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "repo",
    [
        "Owner/Repo",
        "owner/repo",
        "github:owner/repo",
        "owner/repo.git",
        "https://github.com/Owner/Repo",
        "git@github.com:owner/repo.git",
    ],
)
async def test_matching_repo_is_accepted(tmp_path, github_checkout, fetched, repo):
    res = await _create(repo)
    assert res["success"] is True
    assert len(fetched) == 1
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
    registry, fetched, settings, cloned_from
):
    registry["settings"] = settings
    if cloned_from:
        registry["clonedFrom"] = cloned_from
    assert (await _create("team/svc"))["success"] is True


@pytest.mark.parametrize(
    "repo",
    [
        "victim/private-repo",
        "owner/repo-other",
        # same path, another host (review of #1901)
        "https://evil.example/owner/repo",
        "git@evil.example:owner/repo",
        "github:https://evil.example/owner/repo",
        # another provider: none set, so derived from the github.com origin
        "gitlab:owner/repo",
        "azure_devops:owner/repo",
    ],
)
async def test_foreign_repo_is_400_before_any_fetch_or_write(
    tmp_path, github_checkout, fetched, repo
):
    await _rejected(repo)
    assert fetched == []
    assert _specs(tmp_path) == []


async def test_explicit_provider_rejects_another_host_prefix(github_checkout, fetched):
    github_checkout["settings"] = {"gitProvider": "github"}
    await _rejected("gitlab:owner/repo")


async def test_payload_path_is_bound_too(tmp_path, github_checkout, fetched):
    with pytest.raises(HTTPException) as exc:
        await _call(
            FromIssueRequest(
                project_id="p", repo="victim/repo", payload={"number": 1, "title": "x"}
            )
        )
    assert exc.value.status_code == 400
    assert _specs(tmp_path) == []


async def test_no_repo_falls_back_to_the_project(tmp_path, github_checkout, fetched):
    res = await _create(None)
    assert res["success"] is True
    # No declaration: _get_project_provider uses the project's own repo.
    assert fetched == [""]
    assert len(_specs(tmp_path)) == 1


async def test_project_with_no_known_repo_rejects_a_declaration(registry, fetched):
    await _rejected("owner/repo")
    assert fetched == []


@pytest.mark.parametrize(
    "cloned_from",
    [
        "git@ssh.dev.azure.com:v3/Org/Proj/Repo",
        "https://org.visualstudio.com/proj/_git/repo",
        "https://dev.azure.com/org/proj/_git/repo",
    ],
)
async def test_azure_clone_forms_are_own(registry, fetched, cloned_from):
    """Lock-out guard: every Azure clone form names org/proj/repo."""
    registry["clonedFrom"] = cloned_from
    assert (await _create("azure_devops:org/proj/repo"))["success"] is True
    await _rejected("github:org/proj/repo")  # provider derived from the host


@pytest.mark.parametrize(
    ("repo", "ok"),
    [
        ("gitlab:group/proj", True),
        ("gitlab:https://git.example.com/gitlab/group/proj.git", True),
        # unqualified means github (RFC-0020), not this gitlab project
        ("https://git.example.com/gitlab/group/proj.git", False),
        ("https://git.example.com/gitlab/other/proj", False),
        ("https://gitlab.com/group/proj", False),  # same path, another host
    ],
)
async def test_self_hosted_gitlab_under_a_subpath(registry, fetched, repo, ok):
    registry["settings"] = {
        "gitProvider": "gitlab",
        "gitBaseUrl": "https://git.example.com/gitlab",
    }
    registry["clonedFrom"] = "https://git.example.com/gitlab/group/proj.git"
    if ok:
        assert (await _create(repo))["success"] is True
    else:
        await _rejected(repo)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Owner/Repo", ("", "owner/repo")),
        ("https://github.com/Owner/Repo.git", ("github.com", "owner/repo")),
        ("ssh://git@github.com/owner/repo", ("github.com", "owner/repo")),
        ("git@gitlab.com:group/sub/project.git", ("gitlab.com", "group/sub/project")),
        (
            "https://dev.azure.com/org/proj/_git/repo",
            ("dev.azure.com", "org/proj/repo"),
        ),
        ("git@ssh.dev.azure.com:v3/org/proj/repo", ("dev.azure.com", "org/proj/repo")),
        (
            "https://org.visualstudio.com/proj/_git/repo",
            ("dev.azure.com", "org/proj/repo"),
        ),
        ("", ("", "")),
    ],
)
def test_repo_id_normalises_url_forms(value, expected):
    assert from_issue._repo_id(value) == expected


def test_repo_id_strips_the_projects_own_base_path():
    base = ("git.example.com", "gitlab")
    assert from_issue._repo_id("https://git.example.com/gitlab/g/p.git", base) == (
        "git.example.com",
        "g/p",
    )
    # another host keeps its full path
    assert from_issue._repo_id("https://other.example/gitlab/g/p", base) == (
        "other.example",
        "gitlab/g/p",
    )


# --- second review of #1901 -------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "repo", "ok"),
    [
        (None, "group/proj", True),  # no prefix == github
        (None, "github:group/proj", True),
        (None, "gitlab:group/proj", False),  # would send the token to gitlab.com
        (None, "azure_devops:group/proj", False),
        ("https://git.example.com", "gitlab:group/proj", True),
    ],
)
async def test_self_hosted_without_provider_needs_base_url_for_other_prefixes(
    registry, fetched, base_url, repo, ok
):
    """gitProvider unset and a self-hosted host: no provider can be derived."""
    registry["clonedFrom"] = "https://git.example.com/group/proj.git"
    if base_url:
        registry["settings"] = {"gitBaseUrl": base_url}
    if ok:
        assert (await _create(repo))["success"] is True
    else:
        await _rejected(repo)
        assert fetched == []


async def test_vs_ssh_lookalike_host_is_not_azure(registry, fetched):
    registry["clonedFrom"] = "https://dev.azure.com/org/proj/_git/repo"
    await _rejected("azure_devops:git@vs-ssh.evil.example:v3/org/proj/repo")
    assert fetched == []


def test_repo_id_only_collapses_the_real_vs_ssh_host():
    assert from_issue._repo_id("git@vs-ssh.visualstudio.com:v3/o/p/r") == (
        "dev.azure.com",
        "o/p/r",
    )
    assert from_issue._repo_id("git@vs-ssh.evil.example:v3/o/p/r") == (
        "vs-ssh.evil.example",
        "v3/o/p/r",
    )


def _stored_repo(tmp_path: Path) -> str:
    import json

    (spec,) = _specs(tmp_path)
    return json.loads((spec / "requirements.json").read_text())["githubIssue"]["repo"]


@pytest.mark.parametrize(
    ("repo", "stored"),
    [
        ("owner/repo?x=://evil", "owner/repo"),
        ("https://github.com/Owner/Repo.git", "Owner/Repo"),
        # case kept: correlation keys (CFactory, #1775) and dedup compare it
        ("Owner/Repo", "Owner/Repo"),
    ],
)
async def test_the_normalised_repo_is_what_is_stored_and_fetched(
    tmp_path, github_checkout, fetched, repo, stored
):
    await _create(repo)
    assert fetched == [stored]
    assert _stored_repo(tmp_path) == stored
