"""RFC-0019 §3.4 — GET /.well-known/agent-skills/index.json.

Two things must hold, and they are the two an external agent depends on:

1. **It is readable without a credential.** Discovery happens *before* the agent
   holds a token, so these tests run with ``TokenAuthMiddleware`` enforcing (auth
   NOT disabled) and send no ``Authorization`` header.
2. **It matches the contract** — ``apis/agent-skills-manifest.schema.json`` in the
   Factory repo, ``kind: "service"``. That file lives in another repo, so rather
   than depend on it at test time the constraints it imposes are asserted here
   directly (required properties, the skill-id pattern, the per-``kind``
   invocation shape).

The last test is the anti-drift one: the manifest's MCP skills are derived from
``get_tool_definitions()``, so a tool added to or removed from the server has to
show up in the manifest without anyone remembering to edit it.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from server.mcp_remote.tools import get_tool_definitions
from server.routes.well_known import WELL_KNOWN_AGENT_SKILLS_PATH

# The schema's `service_identity.name` / `skill.name` pattern.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

REQUIRED_BY_INVOCATION_KIND = {
    "slash_command": ("command",),
    "mcp_tool": ("tool",),
    "rest": ("method", "path"),
}

# Not a real secret. No request here carries a token at all — the middleware
# never reaches the JWT decode step. Kept off a literal call arg to avoid ruff
# S106 (hardcoded-secret), same as tests/test_scoped_service_tokens.py.
_JWT_SECRET = "unit-test-" + "secret"
_WILDCARD_TOKEN = "not-the-token-" + "under-test"


def _enforcing_settings() -> SimpleNamespace:
    """Settings stand-in with auth ON, so a 200 here means genuinely public."""
    return SimpleNamespace(
        DISABLE_AUTH=False,
        API_TOKEN=_WILDCARD_TOKEN,
        JWT_SECRET=_JWT_SECRET,
        JWT_ALGORITHM="HS256",
    )


@pytest.fixture
def manifest() -> dict[str, Any]:
    """Fetch the manifest with auth enforced and no Authorization header."""
    from server.main import create_app

    app = create_app()
    # Patch the middleware's settings only — create_app() already ran, so this
    # affects request dispatch and nothing else. TestClient is deliberately not
    # entered as a context manager: that would run the lifespan (DB init), which
    # this endpoint does not touch.
    with patch("server.auth.get_settings", _enforcing_settings):
        client = TestClient(app)
        response = client.get(WELL_KNOWN_AGENT_SKILLS_PATH)

    assert response.status_code == 200, (
        "the agent-skills manifest must be readable without authentication "
        f"(RFC-0019 §3.4); got {response.status_code}: {response.text[:200]}"
    )
    body: dict[str, Any] = response.json()
    return body


def test_reachable_unauthenticated_and_json(manifest: dict[str, Any]) -> None:
    """The fixture asserts the 200; here we assert it really is a JSON object."""
    assert isinstance(manifest, dict)
    assert manifest["schema_version"] == "1"
    assert manifest["kind"] == "service"


def test_envelope_matches_service_manifest_contract(manifest: dict[str, Any]) -> None:
    """`service_body` required properties, plus the identity constraints."""
    for key in ("service", "openapi_url", "mcp", "skills"):
        assert key in manifest, f"service manifest is missing required key {key!r}"

    service = manifest["service"]
    assert NAME_PATTERN.match(service["name"]), service["name"]
    assert service["name"] == "aifactory"
    assert service["version"], "service.version must be non-empty"
    # AIFactory is the `act` stage of PARR.
    assert service["role"] == "act"

    # Origin-relative or absolute — the schema's `url` pattern.
    assert manifest["openapi_url"].startswith(("/", "http://", "https://"))

    mcp = manifest["mcp"]
    assert mcp["transport"] in ("stdio", "http+sse", "streamable-http")
    if mcp["transport"] != "stdio":
        assert mcp["endpoint"].startswith(("/", "http://", "https://"))

    # The manifest asserts its own no-auth guarantee rather than leaving a
    # consumer to infer it.
    assert manifest["auth"]["manifest"] == "none"


def test_every_skill_is_well_formed(manifest: dict[str, Any]) -> None:
    """Each skill names one capability and exactly one way to invoke it."""
    skills = manifest["skills"]
    assert len(skills) >= 1, "the schema requires at least one skill"

    names = [s["name"] for s in skills]
    assert len(names) == len(set(names)), f"duplicate skill ids: {names}"

    for skill in skills:
        assert NAME_PATTERN.match(skill["name"]), skill["name"]
        assert skill["description"].strip(), f"{skill['name']} has an empty description"

        invocation = skill["invocation"]
        kind = invocation["kind"]
        assert kind in REQUIRED_BY_INVOCATION_KIND, f"unknown invocation kind {kind!r}"
        for key in REQUIRED_BY_INVOCATION_KIND[kind]:
            assert key in invocation, f"{skill['name']}: {kind} needs {key!r}"

        if kind == "slash_command":
            assert invocation["command"].startswith("/")
        if kind == "rest":
            assert invocation["path"].startswith("/")
            assert invocation["method"] in ("GET", "POST", "PUT", "PATCH", "DELETE")


def test_mcp_skills_track_the_live_tool_catalog(manifest: dict[str, Any]) -> None:
    """Derived, not restated — the manifest cannot drift from the MCP server."""
    advertised = {
        s["invocation"]["tool"]
        for s in manifest["skills"]
        if s["invocation"]["kind"] == "mcp_tool"
    }
    actual = {tool["name"] for tool in get_tool_definitions()}
    assert advertised == actual, (
        "the manifest's mcp_tool skills must be exactly the server's tool "
        f"catalog; missing={actual - advertised} extra={advertised - actual}"
    )


def test_leaks_nothing_sensitive(manifest: dict[str, Any]) -> None:
    """Public metadata only: no credentials, no internal hostnames."""
    blob = json.dumps(manifest).lower()
    for marker in ("secret", "password", "api_token", "bearer ", "acw_", "localhost"):
        assert marker not in blob, f"manifest leaks {marker!r}"
