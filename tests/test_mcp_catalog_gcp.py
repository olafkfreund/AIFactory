"""Unit tests for the GCP MCP catalog entry (V2 — issue #168).

GCP is architecturally distinct from V1/V1.5 entries: it uses HTTP transport
(Google's remote-first design) rather than a local subprocess. These tests
verify:

1. Catalog shape — the ``gcp`` entry is present and uses ``transport="http"``.
2. Marker detection — auto-enabled when project has GCP signals.
3. Credential resolution — ``_probe_gcp`` fires via the catalog's
   ``credential_provider="gcp"`` key.
4. Endpoint override — ``GCP_MCP_ENDPOINT`` env var is honoured at call time.
5. ``build_server_config`` produces the HTTP config dict the SDK expects.
6. Integration with ``get_required_mcp_servers`` — gcp appears when markers
   + creds align; absent when either is missing.

Helm-level tests (secret mount + env var rendering) live in
``tests/helm/test_mcp_credentials_gcp.py``.
"""

from __future__ import annotations

import os

import pytest
from agents.tools_pkg import mcp_catalog
from agents.tools_pkg.mcp_catalog import (
    _GCP_MCP_DEFAULT_ENDPOINT,
    MCPCatalogEntry,
    _get_gcp_mcp_endpoint,
)
from agents.tools_pkg.models import get_required_mcp_servers
from core.mcp_credentials import CredentialStatus

# ---------------------------------------------------------------------------
# Helper: stub get_credential_status for integration tests
# ---------------------------------------------------------------------------


def _stub_creds(monkeypatch, **provider_to_available):
    """Return ``available=True`` for each named provider, False for all others."""

    def fake(provider: str) -> CredentialStatus:
        if provider_to_available.get(provider):
            return CredentialStatus(True, "stub", {})
        return CredentialStatus(False, "stub-none")

    monkeypatch.setattr("core.mcp_credentials.get_credential_status", fake)


# ---------------------------------------------------------------------------
# 1. Catalog shape
# ---------------------------------------------------------------------------


def test_gcp_entry_present():
    """``gcp`` is in the V2 catalog."""
    assert "gcp" in mcp_catalog.catalog_ids()


def test_gcp_entry_transport_is_http():
    """GCP uses HTTP transport — not stdio subprocess."""
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    assert entry.transport == "http", (
        "GCP catalog entry must use transport='http' (Google's remote-first design)"
    )


def test_gcp_entry_has_no_launcher_command():
    """HTTP entries have no subprocess command — launcher_command must be empty."""
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    assert entry.launcher_command == ""


def test_gcp_entry_marker_is_has_gcp():
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    assert entry.marker_capability_keys == ["has_gcp"]


def test_gcp_entry_credential_provider_is_gcp():
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    assert entry.credential_provider == "gcp"


def test_gcp_entry_default_for_coder_and_qa():
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    assert "coder" in entry.default_for_agents
    assert "qa_reviewer" in entry.default_for_agents


def test_gcp_entry_docs_url_points_at_cloud_ai_companion():
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    # Exact URL, not a host substring: the point of the test is that the entry
    # links the Code Assist MCP overview page, which a host-only check cannot
    # tell apart from any other cloud.google.com page.
    assert (
        entry.docs_url == "https://cloud.google.com/gemini/docs/codeassist/mcp-overview"
    )


# ---------------------------------------------------------------------------
# 2. build_server_config — HTTP shape
# ---------------------------------------------------------------------------


def test_gcp_build_server_config_returns_http_type():
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    cfg = entry.build_server_config(creds=None, read_only=True)
    assert cfg.get("type") == "http"


def test_gcp_build_server_config_url_is_default_endpoint(monkeypatch):
    """Without GCP_MCP_ENDPOINT override, the GA default URL is used."""
    monkeypatch.delenv("GCP_MCP_ENDPOINT", raising=False)
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    cfg = entry.build_server_config(creds=None)
    assert cfg["url"] == _GCP_MCP_DEFAULT_ENDPOINT


def test_gcp_build_server_config_no_command_key():
    """HTTP config must NOT have a 'command' key — SDK would reject it."""
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    cfg = entry.build_server_config(creds=None)
    assert "command" not in cfg


def test_gcp_build_server_config_creds_does_not_break():
    """Passing non-None creds to the GCP HTTP entry must not raise."""
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    creds = CredentialStatus(
        True,
        "file:~/.config/gcloud/application_default_credentials.json",
        {"GOOGLE_APPLICATION_CREDENTIALS": "/home/user/.config/gcloud/adc.json"},
    )
    cfg = entry.build_server_config(creds=creds)
    assert cfg["type"] == "http"
    assert "url" in cfg


# ---------------------------------------------------------------------------
# 3. Endpoint override via GCP_MCP_ENDPOINT env var
# ---------------------------------------------------------------------------


def test_gcp_endpoint_override_via_env(monkeypatch):
    """``GCP_MCP_ENDPOINT`` env var overrides the default GA endpoint."""
    custom = "https://cloudaicompanion-staging.googleapis.com/v1/extensions/default/mcp"
    monkeypatch.setenv("GCP_MCP_ENDPOINT", custom)
    assert _get_gcp_mcp_endpoint() == custom


def test_gcp_endpoint_override_blank_env_uses_default(monkeypatch):
    """An empty ``GCP_MCP_ENDPOINT`` string falls back to the default."""
    monkeypatch.setenv("GCP_MCP_ENDPOINT", "")
    assert _get_gcp_mcp_endpoint() == _GCP_MCP_DEFAULT_ENDPOINT


def test_gcp_endpoint_override_whitespace_only_uses_default(monkeypatch):
    """Whitespace-only ``GCP_MCP_ENDPOINT`` is treated as absent."""
    monkeypatch.setenv("GCP_MCP_ENDPOINT", "   ")
    assert _get_gcp_mcp_endpoint() == _GCP_MCP_DEFAULT_ENDPOINT


def test_gcp_endpoint_override_reflected_in_build_server_config(monkeypatch):
    """The override flows through ``build_server_config`` at call time."""
    custom = "https://private-mcp.example.com/gcp/mcp"
    monkeypatch.setenv("GCP_MCP_ENDPOINT", custom)
    entry = mcp_catalog.get_catalog_entry("gcp")
    assert entry is not None
    cfg = entry.build_server_config(creds=None)
    assert cfg["url"] == custom


# ---------------------------------------------------------------------------
# 4. Marker detection via get_required_mcp_servers integration
# ---------------------------------------------------------------------------


def test_gcp_auto_enables_with_has_gcp_marker_and_creds(monkeypatch):
    _stub_creds(monkeypatch, gcp=True)
    servers = get_required_mcp_servers(
        "coder", None, {}, infra_markers={"has_gcp": True}
    )
    assert "gcp" in servers


def test_gcp_skipped_without_has_gcp_marker(monkeypatch):
    """No has_gcp marker → gcp must not appear even if creds are present."""
    _stub_creds(monkeypatch, gcp=True)
    servers = get_required_mcp_servers(
        "coder", None, {}, infra_markers={"has_gcp": False}
    )
    assert "gcp" not in servers


def test_gcp_skipped_when_marker_absent_from_dict(monkeypatch):
    """Marker key not in the dict at all — same result as False."""
    _stub_creds(monkeypatch, gcp=True)
    servers = get_required_mcp_servers("coder", None, {}, infra_markers={})
    assert "gcp" not in servers


def test_gcp_skipped_when_no_creds(monkeypatch):
    """has_gcp marker present but no credentials → gcp stays out."""
    _stub_creds(monkeypatch)  # nothing available
    servers = get_required_mcp_servers(
        "coder", None, {}, infra_markers={"has_gcp": True}
    )
    assert "gcp" not in servers


def test_gcp_force_enable_via_ADD_override_ignores_creds(monkeypatch):
    """ADD override applies regardless of creds — matches existing catalog behaviour."""
    _stub_creds(monkeypatch)  # no creds
    mcp_config = {"AGENT_MCP_coder_ADD": "gcp"}
    servers = get_required_mcp_servers(
        "coder", None, mcp_config, infra_markers={"has_gcp": True}
    )
    assert "gcp" in servers


def test_gcp_force_disable_via_REMOVE_override(monkeypatch):
    _stub_creds(monkeypatch, gcp=True)
    mcp_config = {"AGENT_MCP_coder_REMOVE": "gcp"}
    servers = get_required_mcp_servers(
        "coder", None, mcp_config, infra_markers={"has_gcp": True}
    )
    assert "gcp" not in servers


# ---------------------------------------------------------------------------
# 5. Backward-compat — existing V1 / V1.5 entries unaffected
# ---------------------------------------------------------------------------


def test_existing_stdio_entries_still_produce_command_config():
    """Adding transport/http_endpoint fields must not break V1 stdio entries."""
    for server_id in ("github", "kubernetes", "aws", "azure", "gitlab", "azure_devops"):
        entry = mcp_catalog.get_catalog_entry(server_id)
        assert entry is not None, f"{server_id} not found"
        assert entry.transport == "stdio", f"{server_id}.transport changed"
        creds = CredentialStatus(True, "stub", {"SOME_KEY": "val"})
        cfg = entry.build_server_config(creds, read_only=False)
        assert "command" in cfg, f"{server_id} config missing 'command' key"
        assert "type" not in cfg, f"{server_id} config should NOT have 'type' key"


def test_gcp_is_catalog_server():
    assert mcp_catalog.is_catalog_server("gcp")


def test_catalog_ids_includes_all_entries():
    ids = set(mcp_catalog.catalog_ids())
    assert {
        "github",
        "kubernetes",
        "aws",
        "azure",
        "gitlab",
        "azure_devops",
        "gcp",
    } <= ids
