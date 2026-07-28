#!/usr/bin/env python3
"""Regression tests for #1043 — a configured GitHub token must reach the provider.

Both GitHub-branch call sites used to pass ``kwargs["_token"] = token``. The
gh-CLI ``GitHubProvider`` has no ``_token`` field, so ``get_provider`` raised
``TypeError`` for every project that had configured ``gitToken`` — i.e. exactly
the projects that took the one action needed to make GitHub auth work.

Two assertions per call site, and the SECOND is the one that matters:

1. No ``TypeError``, and the returned provider carries the configured token.
2. The gh-CLI ``GitHubProvider`` is NOT returned. Deleting the token line would
   also stop the ``TypeError`` — but it succeeds under the AMBIENT identity
   (``GH_TOKEN``/``GITHUB_TOKEN`` or the pod's ``gh`` credential store), running
   tenant A's request as somebody else with nothing in the logs to say so. A
   test that only asserts (1) passes that wrong fix, which is why (2) exists.

The GitLab (``_token``) and Azure (``_pat``) branches are correct as written —
those providers really do have those fields — so they are asserted unchanged
here to keep a future "consistency" sweep from breaking them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).parent.parent
for _p in (_ROOT / "apps" / "web-server", _ROOT / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TENANT_TOKEN = "ghp_tenant_scoped_credential_1043"


def _project(**settings):
    return {
        "proj-1043": {
            "name": "tenant-project",
            "path": "/tmp/proj-1043",
            "settings": {
                "gitProvider": "github",
                "gitRepo": "acme/widgets",
                **settings,
            },
        }
    }


def _providers():
    from runners.github.providers.github_provider import GitHubProvider
    from runners.github.providers.http_github_provider import HttpGitHubProvider

    return GitHubProvider, HttpGitHubProvider


def _call_routes_github():
    from server.routes.github import _get_project_provider

    return _get_project_provider("proj-1043")


def _call_auto_fix():
    from server.services.auto_fix_service import _provider_for

    return _provider_for("proj-1043")


@pytest.mark.parametrize(
    "call_site",
    [_call_routes_github, _call_auto_fix],
    ids=["routes.github._get_project_provider", "auto_fix_service._provider_for"],
)
def test_configured_token_reaches_the_provider(call_site):
    """(1) A configured token builds a REST provider carrying that token."""
    _GitHubProvider, HttpGitHubProvider = _providers()

    with patch(
        "server.routes.projects.load_projects",
        return_value=_project(gitToken=TENANT_TOKEN),
    ):
        provider = call_site()

    assert isinstance(provider, HttpGitHubProvider)
    assert provider._token == TENANT_TOKEN


@pytest.mark.parametrize(
    "call_site",
    [_call_routes_github, _call_auto_fix],
    ids=["routes.github._get_project_provider", "auto_fix_service._provider_for"],
)
def test_ambient_gh_cli_cannot_substitute_for_a_configured_token(call_site):
    """(2) With a token configured, the ambient-auth gh-CLI provider is refused.

    This is the assertion that fails the "just drop the ``_token`` line" fix.
    """
    GitHubProvider, _HttpGitHubProvider = _providers()

    with patch(
        "server.routes.projects.load_projects",
        return_value=_project(gitToken=TENANT_TOKEN),
    ):
        provider = call_site()

    assert not isinstance(provider, GitHubProvider), (
        "A configured tenant token produced the gh-CLI provider, which "
        "authenticates from the ambient environment and ignores the token — "
        "the request would run as the wrong identity."
    )


@pytest.mark.parametrize(
    "call_site",
    [_call_routes_github, _call_auto_fix],
    ids=["routes.github._get_project_provider", "auto_fix_service._provider_for"],
)
def test_unconfigured_project_still_falls_through_to_ambient_auth(call_site):
    """No token configured -> gh CLI, unchanged. Deliberate, not an oversight."""
    GitHubProvider, HttpGitHubProvider = _providers()

    with patch("server.routes.projects.load_projects", return_value=_project()):
        provider = call_site()

    assert isinstance(provider, GitHubProvider)
    assert not isinstance(provider, HttpGitHubProvider)


@pytest.mark.parametrize(
    "call_site",
    [_call_routes_github, _call_auto_fix],
    ids=["routes.github._get_project_provider", "auto_fix_service._provider_for"],
)
@pytest.mark.parametrize(
    ("provider_name", "module", "cls_name"),
    [
        ("gitlab", "runners.github.providers.gitlab_provider", "GitLabProvider"),
        (
            "azure_devops",
            "runners.github.providers.azure_devops_provider",
            "AzureDevOpsProvider",
        ),
    ],
)
def test_sibling_branches_keep_their_own_token_field(
    call_site, provider_name, module, cls_name
):
    """GitLab ``_token`` / Azure ``_pat`` are correct — a sweep must not "fix" them."""
    import importlib

    cls = getattr(importlib.import_module(module), cls_name)

    with patch(
        "server.routes.projects.load_projects",
        return_value=_project(
            gitProvider=provider_name,
            gitToken=TENANT_TOKEN,
            gitOrg="acme",
            gitProject="widgets",
        ),
    ):
        provider = call_site()

    assert isinstance(provider, cls)
    assert getattr(provider, "_token", None) == TENANT_TOKEN or (
        getattr(provider, "_pat", None) == TENANT_TOKEN
    )
