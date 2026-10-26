"""Autonomy ceiling and agent identity on ``acw_`` keys (Factory#1960 slice 1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_WEB_SERVER_DIR = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER_DIR))

from server.database.models import ApiKey  # noqa: E402
from server.routes.api_keys import CreateApiKeyRequest  # noqa: E402

_VERSIONS = _WEB_SERVER_DIR / "server" / "database" / "alembic" / "versions"


def test_key_with_ceiling_carries_it_and_its_agent_identity():
    key = ApiKey(id="k-123", autonomy_ceiling="medium")
    assert key.autonomy_ceiling == "medium"
    assert key.agent_identity == "agent:aifactory/k-123"


def test_key_without_ceiling_records_null_not_a_default():
    key = ApiKey(id="k-456")
    assert key.autonomy_ceiling is None
    assert key.agent_identity == "agent:aifactory/k-456"


def test_create_request_ceiling_is_optional_and_validated():
    assert CreateApiKeyRequest(name="n", org_id="o").autonomy_ceiling is None
    req = CreateApiKeyRequest(name="n", org_id="o", autonomy_ceiling="hard")
    assert req.autonomy_ceiling == "hard"
    with pytest.raises(ValidationError):
        CreateApiKeyRequest(name="n", org_id="o", autonomy_ceiling="unlimited")


def test_migration_chains_onto_a_single_head():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(_VERSIONS.parent))
    assert script.get_heads() == ["7a1f3c9e2b50"]
    assert script.get_revision("7a1f3c9e2b50").down_revision == "5e2d9c1a7b40"
