"""
MCP Server Catalog
==================

Declarative catalog of "default" MCP servers AIFactory ships out of the box.

Each entry says:
- *what* to launch (subprocess command/args, or HTTP endpoint)
- *when* to enable (project markers from ``project_context.detect_infra_markers``)
- *how* to authenticate (key into ``core.mcp_credentials.get_credential_status``)
- *for whom* (which agent types receive the tools by default)

The runtime decision is made in two layers:
1. ``get_required_mcp_servers()`` (models.py) walks the catalog and includes a
   server ID in the agent's name list IFF agent + markers + creds all align.
2. ``client.py`` then asks the catalog for the actual subprocess config dict
   to pass to ``ClaudeAgentOptions(mcp_servers=...)``.

Why a catalog vs. inline if-blocks? The four entries here (GitHub, K8s, AWS,
Azure) all follow the same shape — launcher + markers + creds — and the next
batch (GitLab, ADO, GCP) will too. A data structure beats six more
copy-pasted if-blocks in client.py.

Transport types
---------------
``transport = "stdio"``  (default)
    Spawns a local subprocess. ``launcher_command`` + ``launcher_args`` are
    the argv. The Claude Agent SDK config dict looks like::

        {"command": "npx", "args": [...], "env": {...}}

``transport = "http"``
    Connects to a remote MCP endpoint over HTTP/SSE. ``http_endpoint`` is
    the base URL resolved at build time (so env-var overrides apply). The
    SDK config dict::

        {"type": "http", "url": "..."}

V1 entries: github, kubernetes, aws, azure — the four mature, ship-today
options as of 2026-05.

V1.5 entries: gitlab, azure_devops — shipped behind V1 in the same file.

V2 entries: gcp — remote-first HTTP transport (Google's design). Added in
PR for issue #168. Google Cloud AI Companion MCP went GA in March 2026.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from core.mcp_credentials import CredentialStatus

# ---------------------------------------------------------------------------
# Default GCP MCP endpoint (GA, March 2026).
#
# Google publishes the Cloud AI Companion MCP server at the endpoint below.
# This is the canonical GA URL documented at
# https://cloud.google.com/gemini/docs/codeassist/mcp-overview
#
# Operators running against a staging project, VPC Service Controls
# perimeter, or a private endpoint MUST override this via the
# ``GCP_MCP_ENDPOINT`` env var (or ``mcpCredentials.gcp.endpointOverride``
# Helm value which maps to the same env var).
#
# Why this endpoint and not one of the 50+ other Google MCP servers?
# Cloud AI Companion (formerly Gemini Code Assist) is the closest analogue
# to what GitHub Copilot / Azure AI / AWS Bedrock Agents expose for
# developer workflows: code context, GCP resource enumeration, and IAM
# query. It is the only one Google formally published an MCP URL for in
# the March 2026 GA announcement. Other GCP-specific servers (e.g. BigQuery
# Explorer, Cloud Run Deployer) remain in private preview as of 2026-05;
# operators can override the endpoint to use them when they GA.
_GCP_MCP_DEFAULT_ENDPOINT = (
    "https://cloudaicompanion.googleapis.com/v1/extensions/default/mcp"
)


def _get_gcp_mcp_endpoint() -> str:
    """Return the effective GCP MCP endpoint.

    Operator override via ``GCP_MCP_ENDPOINT`` wins; falls back to the GA
    default. Evaluated at call time (not import time) so tests and runtime
    operators can set the env var after module load.
    """
    return os.environ.get("GCP_MCP_ENDPOINT", "").strip() or _GCP_MCP_DEFAULT_ENDPOINT


@dataclass(frozen=True)
class MCPCatalogEntry:
    """One default MCP server.

    The ``id`` matches the key used in ``ClaudeAgentOptions(mcp_servers={...})``
    AND the name accepted by ``AGENT_MCP_<agent>_ADD/REMOVE`` overrides — so
    operators can force-disable a catalog server per project with the same
    mechanism they already use for context7/playwright.
    """

    id: str
    """Stable identifier. Also the dict key the Claude SDK sees."""

    # --- stdio transport (default) ---

    launcher_command: str = ""
    """First element of the subprocess argv (e.g. "npx", "uvx"). Empty for
    HTTP-transport entries."""

    launcher_args: list[str] = field(default_factory=list)
    """Remaining argv. ``readonly_args`` are appended when read-only mode is on."""

    readonly_args: list[str] = field(default_factory=list)
    """Extra argv appended in read-only mode. Empty if the server doesn't have
    a flag (then we rely on IAM-side read-only roles documented in the docs
    page)."""

    # --- http transport ---

    transport: str = "stdio"
    """Transport mechanism: "stdio" (subprocess) or "http" (remote MCP endpoint)."""

    http_endpoint: str = ""
    """Base URL for HTTP-transport entries. Ignored for stdio entries.

    For GCP, leave empty — ``_build_http_config`` calls
    ``_get_gcp_mcp_endpoint()`` to honour the ``GCP_MCP_ENDPOINT`` env
    override at call time rather than at import time. Static endpoints
    (future non-GCP HTTP servers) can set this directly.
    """

    # --- shared fields ---

    marker_capability_keys: list[str] = field(default_factory=list)
    """Capability flags from ``detect_infra_markers()`` — ANY match enables the
    server. Empty list = always-on (used for github where every repo qualifies).
    """

    credential_provider: str = ""
    """Key passed to ``mcp_credentials.get_credential_status()``. Empty = no
    credentials required (none of the V1 entries hit this path; reserved for
    future no-auth servers)."""

    default_for_agents: list[str] = field(default_factory=list)
    """Agent types that get this server by default. Other agents need an
    explicit ``AGENT_MCP_<agent>_ADD`` to receive it."""

    docs_url: str = ""
    """Pointer the docs page links out to. Also useful for log messages."""

    def build_server_config(
        self, creds: CredentialStatus | None, read_only: bool = True
    ) -> dict[str, Any]:
        """Materialize the dict the Claude Agent SDK expects.

        Caller is responsible for having already checked credentials (and that
        the agent should get this server). This function is pure — it just
        translates the entry's transport + creds into the SDK's config schema.

        stdio entries produce::

            {"command": "npx", "args": [...], "env": {...}}

        http entries produce::

            {"type": "http", "url": "..."}

        The GCP HTTP entry relies on ``GOOGLE_APPLICATION_CREDENTIALS`` being
        set in the pod's environment (the Helm template does this when
        ``mcpCredentials.providers.gcp=true``). The Cloud AI Companion server
        performs its own ADC exchange — AIFactory does not mint tokens.
        """
        if self.transport == "http":
            return self._build_http_config(creds)
        return self._build_stdio_config(creds, read_only)

    def _build_stdio_config(
        self, creds: CredentialStatus | None, read_only: bool
    ) -> dict[str, Any]:
        args = list(self.launcher_args)
        if read_only:
            args = args + list(self.readonly_args)
        config: dict[str, Any] = {"command": self.launcher_command, "args": args}
        if creds and creds.env_vars:
            config["env"] = dict(creds.env_vars)
        return config

    def _build_http_config(self, creds: CredentialStatus | None) -> dict[str, Any]:
        # Resolve endpoint — falls back to _get_gcp_mcp_endpoint() which
        # honours the GCP_MCP_ENDPOINT env override at call time. Future
        # non-GCP HTTP entries that set http_endpoint directly bypass this.
        endpoint = self.http_endpoint or _get_gcp_mcp_endpoint()
        config: dict[str, Any] = {"type": "http", "url": endpoint}
        # creds accepted for signature uniformity with _build_stdio_config.
        # The GCP entry does not embed tokens in the config dict — Cloud AI
        # Companion reads GOOGLE_APPLICATION_CREDENTIALS from the environment
        # directly and performs the ADC exchange internally.
        return config


# ---------------------------------------------------------------------------
# V1 catalog — 2026-05 mature, ship-today set
# ---------------------------------------------------------------------------
#
# Pinning policy:
# - GitHub:     Anthropic-maintained reference implementation; track latest.
# - Kubernetes: PIN ">=3.6.0" — earlier versions have CVE-2026-46519 (CVSS 8.8)
#               where the --read-only flag is bypass-able at the execution
#               layer. v3.6.0+ fixed this. Defense in depth: the kubeconfig
#               itself should use a read-only ServiceAccount/RBAC; do not
#               rely on the flag alone.
# - AWS:        AWS Labs ships as a uvx-installable Python package (no npm).
# - Azure:      Microsoft-official, 2.0 stable as of 2026-04. ~276 tools.

CATALOG: list[MCPCatalogEntry] = [
    MCPCatalogEntry(
        id="github",
        launcher_command="npx",
        launcher_args=["-y", "@modelcontextprotocol/server-github"],
        marker_capability_keys=[],  # always-on if creds present (every repo is "a repo")
        credential_provider="github",
        readonly_args=[],
        default_for_agents=[
            "coder",
            "planner",
            "qa_reviewer",
            "pr_reviewer",
            "spec_gatherer",
        ],
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    ),
    MCPCatalogEntry(
        id="kubernetes",
        launcher_command="npx",
        launcher_args=["-y", "kubernetes-mcp-server@>=3.6.0"],
        marker_capability_keys=["has_kubernetes"],
        credential_provider="kubernetes",
        readonly_args=["--read-only"],
        default_for_agents=["coder", "qa_reviewer"],
        docs_url="https://github.com/containers/kubernetes-mcp-server",
    ),
    MCPCatalogEntry(
        id="aws",
        launcher_command="uvx",
        launcher_args=["awslabs.aws-api-mcp-server@latest"],
        marker_capability_keys=["has_aws"],
        credential_provider="aws",
        readonly_args=[],  # no native flag; require read-only IAM role in docs
        default_for_agents=["coder", "qa_reviewer"],
        docs_url="https://awslabs.github.io/mcp/",
    ),
    MCPCatalogEntry(
        id="azure",
        launcher_command="npx",
        launcher_args=["-y", "@azure/mcp@latest", "server", "start"],
        marker_capability_keys=["has_azure"],
        credential_provider="azure",
        readonly_args=[],  # no native flag; require Reader-role principal in docs
        default_for_agents=["coder", "qa_reviewer"],
        docs_url="https://devblogs.microsoft.com/azure-sdk/announcing-azure-mcp-server-2-0-stable-release/",
    ),
    # ---- V1.5: GitLab + Azure DevOps -----------------------------------
    #
    # GitLab: NO proper canonical npm package exists as of 2026-05.  The
    # ``@modelcontextprotocol/server-gitlab`` package is too immature, so
    # we ship the community fork ``@zereight/mcp-gitlab`` (72+ tools,
    # actively shipping releases).  The vendor-disclosure note lives in
    # docs/docs/concepts/mcp-servers.md — this catalog entry IS the
    # disclosure: the launcher arg names the vendor explicitly so
    # operators see what they're opting into.
    MCPCatalogEntry(
        id="gitlab",
        launcher_command="npx",
        launcher_args=["-y", "@zereight/mcp-gitlab@latest"],
        marker_capability_keys=["has_gitlab_ci"],
        credential_provider="gitlab",
        readonly_args=[],  # rely on PAT scope (read_api, read_repository)
        default_for_agents=["coder", "qa_reviewer"],
        docs_url="https://github.com/zereight/gitlab-mcp",
    ),
    # Azure DevOps: Microsoft's local ``@azure-devops/mcp@next`` server
    # works today but is being phased out in favour of an Azure DevOps
    # Remote MCP HTTP server (public preview since 2026-03).  We ship the
    # local version with a sunset notice in the docs — a follow-up Epic
    # will migrate to Remote MCP once it goes GA.
    MCPCatalogEntry(
        id="azure_devops",
        launcher_command="npx",
        launcher_args=["-y", "@azure-devops/mcp@next"],
        marker_capability_keys=["has_azure_devops"],
        credential_provider="azure_devops",
        readonly_args=[],  # rely on PAT scope (read-only)
        default_for_agents=["coder", "qa_reviewer"],
        docs_url="https://github.com/microsoft/azure-devops-mcp",
    ),
    # ---- V2: GCP -------------------------------------------------------
    #
    # GCP uses a remote-first HTTP transport (Google's design) rather than
    # a local subprocess. Cloud AI Companion MCP went GA in March 2026 and
    # is the canonical developer-workflow server — code context, GCP
    # resource enumeration, IAM query. Closes issue #168 / Epic #100.
    #
    # Endpoint: the default is _GCP_MCP_DEFAULT_ENDPOINT (Cloud AI Companion
    # GA URL). Operator override via ``GCP_MCP_ENDPOINT`` env var (set by
    # the Helm template when ``mcpCredentials.gcp.endpointOverride`` is
    # non-empty). ``_get_gcp_mcp_endpoint()`` is evaluated at build time.
    #
    # Auth: Application Default Credentials. ``GOOGLE_APPLICATION_CREDENTIALS``
    # must be set in the pod environment — either by the Helm template
    # (providers.gcp=true mounts the SA JSON and sets the env var) or by
    # Workload Identity Federation (which sets it via the projected token
    # path automatically). The Cloud AI Companion server performs its own ADC
    # exchange; AIFactory does not mint OAuth2 tokens.
    MCPCatalogEntry(
        id="gcp",
        transport="http",
        # http_endpoint left empty — _build_http_config falls through to
        # _get_gcp_mcp_endpoint() which reads GCP_MCP_ENDPOINT at call time.
        http_endpoint="",
        marker_capability_keys=["has_gcp"],
        credential_provider="gcp",
        default_for_agents=["coder", "qa_reviewer"],
        docs_url="https://cloud.google.com/gemini/docs/codeassist/mcp-overview",
    ),
]


_CATALOG_BY_ID: dict[str, MCPCatalogEntry] = {entry.id: entry for entry in CATALOG}


def get_catalog_entry(server_id: str) -> MCPCatalogEntry | None:
    """Lookup helper for client.py / models.py / mcp_doctor CLI."""
    return _CATALOG_BY_ID.get(server_id)


def is_catalog_server(server_id: str) -> bool:
    """Cheap "do we ship this server?" check used by name-mapping in models.py."""
    return server_id in _CATALOG_BY_ID


def catalog_ids() -> list[str]:
    """Stable ordering — matches CATALOG list above."""
    return [entry.id for entry in CATALOG]
