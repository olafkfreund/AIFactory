#!/usr/bin/env python3
"""
Unit tests for GitProvider.assign_to_user (V1-A, issue #93).

Covers:
- GitHubProvider: GraphQL replaceActorsForAssignable happy path, the
  "Copilot" alias → copilot-swe-agent translation, and silent no-op when
  Copilot is disabled at org level.
- GitLabProvider: NotImplementedError stub (V1; Duo Workflow lands in V1.5).
- AzureDevOpsProvider: NotImplementedError stub (permanent — no agent).
"""

from __future__ import annotations

import json
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_github_provider(graphql_responses: list[dict]) -> GitHubProvider:
    """Build a GitHubProvider with a mocked gh_client.run().

    Passes `_gh_client` directly so `__post_init__` skips real GHClient
    construction (which would otherwise pull in subprocess + logging
    setup that pollutes downstream tests' caplog state).

    `graphql_responses` is the ordered list of dicts each call should
    return. Each is wrapped in a stub mimicking GHCommandResult so the
    provider's `result.stdout` access works.
    """
    def _stub(resp_dict: dict):
        stub = MagicMock()
        stub.stdout = json.dumps(resp_dict)
        return stub

    mock_client = MagicMock()
    mock_client.run = AsyncMock(side_effect=[_stub(r) for r in graphql_responses])
    return GitHubProvider(_repo="acme/widgets", _gh_client=mock_client)


# ---------------------------------------------------------------------------
# GitHub — happy path: "Copilot" → copilot-swe-agent gets assigned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_assign_copilot_happy_path():
    suggested_actors = {
        "data": {
            "repository": {
                "id": "REPO_NODE_ID",
                "suggestedActors": {
                    "nodes": [
                        {
                            "login": "copilot-swe-agent",
                            "__typename": "Bot",
                            "id": "BOT_NODE_ID",
                        },
                        {
                            "login": "alice",
                            "__typename": "User",
                            "id": "USER_NODE_ALICE",
                        },
                    ]
                },
            }
        }
    }
    issue_resp = {
        "data": {"repository": {"issue": {"id": "ISSUE_NODE_ID"}}}
    }
    mutation_resp = {
        "data": {
            "replaceActorsForAssignable": {"assignable": {"number": 42}}
        }
    }

    provider = _make_github_provider(
        [suggested_actors, issue_resp, mutation_resp]
    )
    await provider.assign_to_user(42, ["Copilot"])

    # Three gh api graphql calls in order: actors lookup, issue lookup, mutation.
    calls = provider._gh_client.run.await_args_list
    assert len(calls) == 3, f"expected 3 GraphQL calls, got {len(calls)}"

    # Last call is the mutation — verify it sends the right node IDs.
    mutation_cmd = calls[2].args[0]
    assert "graphql" in mutation_cmd
    flat = " ".join(mutation_cmd)
    assert "replaceActorsForAssignable" in flat
    assert "assignableId=ISSUE_NODE_ID" in flat
    assert "actorIds[]=BOT_NODE_ID" in flat


# ---------------------------------------------------------------------------
# GitHub — silent no-op when Copilot isn't in suggestedActors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_assign_copilot_silent_noop_when_disabled():
    suggested_actors_no_copilot = {
        "data": {
            "repository": {
                "id": "REPO_NODE_ID",
                "suggestedActors": {
                    "nodes": [
                        {
                            "login": "alice",
                            "__typename": "User",
                            "id": "USER_NODE_ALICE",
                        }
                    ]
                },
            }
        }
    }
    provider = _make_github_provider([suggested_actors_no_copilot])
    await provider.assign_to_user(42, ["Copilot"])

    # Only the actor lookup should fire — no issue lookup, no mutation,
    # no exception raised. The tracker detects the no-op by re-fetching.
    assert provider._gh_client.run.await_count == 1


# ---------------------------------------------------------------------------
# GitHub — case-insensitive alias, mixed with a real username
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_assign_copilot_alias_case_insensitive_and_mixed():
    suggested_actors = {
        "data": {
            "repository": {
                "id": "REPO_NODE_ID",
                "suggestedActors": {
                    "nodes": [
                        {
                            "login": "copilot-swe-agent",
                            "__typename": "Bot",
                            "id": "BOT_NODE_ID",
                        },
                        {
                            "login": "alice",
                            "__typename": "User",
                            "id": "USER_NODE_ALICE",
                        },
                    ]
                },
            }
        }
    }
    issue_resp = {
        "data": {"repository": {"issue": {"id": "ISSUE_NODE_ID"}}}
    }
    mutation_resp = {"data": {"replaceActorsForAssignable": {}}}

    provider = _make_github_provider(
        [suggested_actors, issue_resp, mutation_resp]
    )
    await provider.assign_to_user(42, ["copilot", "alice"])

    mutation_cmd = provider._gh_client.run.await_args_list[2].args[0]
    flat = " ".join(mutation_cmd)
    # Both actor IDs should be in the mutation payload.
    assert "actorIds[]=BOT_NODE_ID" in flat
    assert "actorIds[]=USER_NODE_ALICE" in flat


# ---------------------------------------------------------------------------
# GitHub — empty assignees is a no-op (no API calls at all)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_assign_empty_list_is_noop():
    provider = _make_github_provider([])
    await provider.assign_to_user(42, [])
    assert provider._gh_client.run.await_count == 0


# ---------------------------------------------------------------------------
# GitLab — V1 stub raises NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_assign_to_user_raises_notimplemented():
    provider = GitLabProvider(_repo="acme/widgets", _token="dummy")
    with pytest.raises(NotImplementedError) as exc:
        await provider.assign_to_user(42, ["Copilot"])
    msg = str(exc.value)
    assert "V1.5" in msg or "#98" in msg
    assert "duo_workflows" in msg.lower()


# ---------------------------------------------------------------------------
# Azure DevOps — permanent stub raises NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_azure_devops_assign_to_user_raises_notimplemented():
    provider = AzureDevOpsProvider(_repo="my-repo", _pat="dummy")
    with pytest.raises(NotImplementedError) as exc:
        await provider.assign_to_user(42, ["Copilot"])
    msg = str(exc.value)
    assert "Azure DevOps" in msg
    assert "no autonomous coding agent" in msg.lower()
