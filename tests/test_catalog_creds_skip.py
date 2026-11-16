"""Catalog MCP servers the sandboxed agent env cannot give a credential to (Factory#3720)."""

from __future__ import annotations

import pytest
from agents.tools_pkg.mcp_catalog import get_catalog_entry, unusable_credential_reason
from core.mcp_credentials import CredentialStatus


@pytest.mark.parametrize(
    "sid", ["aws", "azure", "kubernetes", "github", "gitlab", "azure_devops"]
)
def test_probe_hit_without_env_vars_is_skipped(sid):
    creds = CredentialStatus(True, "env:AWS_ACCESS_KEY_ID")
    why = unusable_credential_reason(get_catalog_entry(sid), creds)
    assert why is not None
    assert "env:AWS_ACCESS_KEY_ID" in why


@pytest.mark.parametrize("sid", ["github", "azure", "aws"])
def test_probe_hit_with_env_vars_is_kept(sid):
    creds = CredentialStatus(True, "service-principal", {"X": "secret-value"})
    assert unusable_credential_reason(get_catalog_entry(sid), creds) is None


def test_http_server_is_kept():
    assert (
        unusable_credential_reason(
            get_catalog_entry("gcp"), CredentialStatus(True, "f")
        )
        is None
    )


def test_reason_never_carries_a_value():
    creds = CredentialStatus(True, "k8s-in-cluster", {})
    assert "secret" not in (
        unusable_credential_reason(get_catalog_entry("kubernetes"), creds) or ""
    )
