"""Tests for GitProvider.enable_auto_merge (RFC-0011 #637).

GitHub enables native auto-merge via ``gh pr merge --auto``; GitLab/ADO raise a
clear NotImplementedError for now.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from runners.github.providers.azure_devops_provider import (
    AzureDevOpsProvider,  # noqa: E402
)
from runners.github.providers.github_provider import GitHubProvider  # noqa: E402
from runners.github.providers.gitlab_provider import GitLabProvider  # noqa: E402


@pytest.mark.asyncio
async def test_github_enable_auto_merge_squash():
    client = MagicMock()
    client.run = AsyncMock(return_value=MagicMock(stdout=""))
    provider = GitHubProvider(_repo="acme/widgets", _gh_client=client)

    ok = await provider.enable_auto_merge(42)
    assert ok is True
    cmd = client.run.await_args_list[0].args[0]
    assert cmd[:3] == ["pr", "merge", "42"]
    assert "--auto" in cmd
    assert "--squash" in cmd


@pytest.mark.asyncio
async def test_github_enable_auto_merge_method_variants():
    client = MagicMock()
    client.run = AsyncMock(return_value=MagicMock(stdout=""))
    provider = GitHubProvider(_repo="acme/widgets", _gh_client=client)

    await provider.enable_auto_merge(1, merge_method="rebase")
    assert "--rebase" in client.run.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_github_enable_auto_merge_returns_false_on_error():
    client = MagicMock()
    client.run = AsyncMock(side_effect=RuntimeError("auto-merge not allowed"))
    provider = GitHubProvider(_repo="acme/widgets", _gh_client=client)
    assert await provider.enable_auto_merge(7) is False


@pytest.mark.asyncio
async def test_gitlab_enable_auto_merge_not_implemented():
    provider = GitLabProvider(_repo="acme/widgets", _token="t")
    with pytest.raises(NotImplementedError) as exc:
        await provider.enable_auto_merge(1)
    assert "GitLab" in str(exc.value)


@pytest.mark.asyncio
async def test_ado_enable_auto_merge_not_implemented():
    provider = AzureDevOpsProvider(_repo="my-repo", _pat="dummy")
    with pytest.raises(NotImplementedError) as exc:
        await provider.enable_auto_merge(1)
    assert "Azure DevOps" in str(exc.value)
