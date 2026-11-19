"""Project identity helpers are deterministic across legacy URL forms."""

import pytest
from server.project_identity import (
    SLUG_MAX,
    SLUG_RE,
    derive_slug,
    normalize_repo_identity,
    unique_slug,
    validate_slug,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Hello, World!", "hello-world"),
        ("--A___B  C--", "a-b-c"),
        ("", "project"),
        ("🚀🌍", "project"),
        ("A" * 80, "a" * 56),
        ("A" * 55 + "!" + "B" * 30, "a" * 55),
    ],
)
def test_derive_slug(name: str, expected: str) -> None:
    assert derive_slug(name) == expected


@pytest.mark.parametrize(
    ("slug", "valid"),
    [
        ("a", True),
        ("a-2", True),
        ("a" * SLUG_MAX, True),
        ("", False),
        ("A", False),
        ("a_b", False),
        ("-a", False),
        ("a-", False),
        ("a--b", False),
        ("a" * (SLUG_MAX + 1), False),
    ],
)
def test_validate_slug(slug: str, valid: bool) -> None:
    assert validate_slug(slug) is valid
    assert (SLUG_RE.fullmatch(slug) is not None and len(slug) <= SLUG_MAX) is valid


@pytest.mark.parametrize(
    ("base", "taken", "expected"),
    [
        ("project", set(), "project"),
        ("project", {"project"}, "project-2"),
        ("project", {"project", "project-2"}, "project-3"),
        ("a" * 64, {"a" * 64}, "a" * 62 + "-2"),
        ("a" * 80, set(), "a" * 64),
    ],
)
def test_unique_slug(base: str, taken: set[str], expected: str) -> None:
    result = unique_slug(base, taken)
    assert result == expected
    assert len(result) <= SLUG_MAX


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://GitHub.com/Org/Repo.git", "github.com/org/repo"),
        ("https://GitHub.com/Org/Repo.GIT", "github.com/org/repo"),
        ("http://github.com:8443/Org/Repo/", "github.com/org/repo"),
        ("ssh://git@GitLab.com:2222/A/B/C/Repo.git", "gitlab.com/a/b/c/repo"),
        ("git://GitHub.com/Org/Repo.git", "github.com/org/repo"),
        ("git@GitLab.com:A/B/C/Repo.git", "gitlab.com/a/b/c/repo"),
        ("https://user:token@GitHub.com:443/Org/Repo.git", "github.com/org/repo"),
        (None, None),
        ("", None),
        ("/home/user/repo", None),
        ("./repo", None),
        ("../repo", None),
        ("~/repo", None),
        ("file:///home/user/repo", None),
        ("https://github.com/", None),
        ("github.com/org/repo", None),
    ],
)
def test_normalize_repo_identity(url: str | None, expected: str | None) -> None:
    assert normalize_repo_identity(url) == expected
