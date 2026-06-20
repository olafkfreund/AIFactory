"""
MCP Credentials Chain
=====================

Single source of truth for "do we have credentials for provider X?"

Resolution order (first hit wins):
  1. Operator-level ~/.aifactory/mcp-credentials.json (references env vars / files,
     NEVER stores secrets in plain text).
  2. Cloud-CLI native discovery — the files and env vars each provider's official
     CLI reads (~/.aws/credentials, ~/.kube/config, GOOGLE_APPLICATION_CREDENTIALS,
     etc.).
  3. K8s in-cluster identity — service account token, IRSA, Workload Identity.

Probes are cheap and do NOT validate that credentials WORK — only that something
*could* be picked up by the MCP server subprocess. If a probe says "yes" but the
creds are stale, the MCP server's own startup surfaces the failure as a tool-side
error during the agent run. Acceptable trade-off: API-validation probes would
add latency and rate-limit pressure every spawn.

Used by ``agents.tools_pkg.mcp_catalog`` to decide which servers to auto-enable
per project; the catalog walks the chain via ``get_credential_status()`` and
skips any server whose creds are missing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

OPERATOR_CONFIG_PATH = Path.home() / ".aifactory" / "mcp-credentials.json"
# RFC-0013 (#644): operator-declared MCP servers (the deployment-metrics MCP and
# any extra env-MCPs). Same env-reference posture as mcp-credentials.json — it
# names env vars, it does NOT store secrets, and secrets never reach argv.
OPERATOR_MCP_SERVERS_PATH = Path.home() / ".aifactory" / "mcp-servers.json"
K8S_SERVICE_ACCOUNT_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


@dataclass(frozen=True)
class CredentialStatus:
    """Outcome of a credential probe for one MCP provider.

    ``available`` is the truthiness check the catalog cares about. ``source``
    is a short tag suitable for logs and the ``aifactory mcp-doctor`` output
    ("operator-config", "env:GITHUB_TOKEN", "file:~/.kube/config", "irsa", ...).
    ``env_vars`` is the dict the MCP subprocess should be launched with —
    typically maps the canonical env names the server's docs document.
    """

    available: bool
    source: str
    env_vars: dict[str, str] = field(default_factory=dict)


def _read_perm_safe_json(path: Path) -> dict:
    """Read a JSON config that must be 0600-or-stricter, else refuse.

    Same posture Claude Code takes for ~/.claude/.credentials.json: anything with
    group/other read bits is unsafe (these files name credential env vars).
    Returns an empty dict if the file is missing, unreadable, or perm-checked out.
    """
    if not path.exists():
        return {}
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            logger.warning(
                "Refusing to read %s with permissions %o (must be 0600 or stricter)",
                path,
                mode,
            )
            return {}
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}


@lru_cache(maxsize=1)
def _load_operator_config() -> dict:
    """Read ~/.aifactory/mcp-credentials.json if present (perm-safe, cached)."""
    return _read_perm_safe_json(OPERATOR_CONFIG_PATH)


@lru_cache(maxsize=1)
def _load_operator_mcp_servers() -> dict:
    """Read ~/.aifactory/mcp-servers.json if present (perm-safe, cached).

    RFC-0013 (#644). Shape (an open object; unknown keys ignored)::

        {
          "servers": {
            "deployment-metrics": {
              "command": "python3",
              "args": ["/opt/aifactory/deployment_metrics_stub.py"],
              "markers": [],            // capability flags; [] == always-on
              "agents": ["planner", "coder", "qa_reviewer"],
              "env": {"DORA_PROVIDER": "DORA_PROVIDER"},  // name -> env-var NAME
              "docs_url": "..."
            }
          }
        }

    ``env`` maps a target env-var name to the NAME of an env var to read at build
    time — secrets are resolved from the process environment, never stored here
    and never placed on argv. Returns ``{}`` when absent / unreadable.
    """
    return _read_perm_safe_json(OPERATOR_MCP_SERVERS_PATH)


def get_operator_mcp_servers() -> dict[str, dict]:
    """Return the operator-declared MCP servers keyed by id.

    Only well-formed entries (a dict with a non-empty string ``command``, or an
    ``http`` transport with a ``url``) are returned; anything malformed is
    dropped with a warning so a bad config degrades rather than crashes.
    """
    raw = _load_operator_mcp_servers().get("servers")
    if not isinstance(raw, dict):
        return {}
    servers: dict[str, dict] = {}
    for server_id, spec in raw.items():
        if not isinstance(server_id, str) or not isinstance(spec, dict):
            logger.warning("Skipping malformed MCP server entry: %r", server_id)
            continue
        transport = str(spec.get("transport", "stdio")).strip().lower()
        if transport == "http":
            if not str(spec.get("url", "")).strip():
                logger.warning("Skipping http MCP server %r with no url", server_id)
                continue
        elif not str(spec.get("command", "")).strip():
            logger.warning("Skipping stdio MCP server %r with no command", server_id)
            continue
        servers[server_id] = spec
    return servers


def resolve_operator_mcp_env(spec: dict) -> dict[str, str]:
    """Resolve an operator server's ``env`` map (name -> env-var NAME) to values.

    Reads each referenced env var from the process environment. Unset/empty
    references are omitted (degrade, never fabricate). Secrets are therefore
    passed via the subprocess environment — never via argv.
    """
    env_map = spec.get("env")
    if not isinstance(env_map, dict):
        return {}
    resolved: dict[str, str] = {}
    for target_name, env_ref in env_map.items():
        value = _resolve_env(str(env_ref)) if env_ref else None
        if value is not None:
            resolved[str(target_name)] = value
    return resolved


def _resolve_env(env_name: str | None) -> str | None:
    """Return the value of the named env var, or None if unset/empty."""
    if not env_name:
        return None
    value = os.environ.get(env_name)
    return value if value else None


def _file_exists(path_str: str | None) -> bool:
    """Return True if the path resolves to an existing readable file."""
    if not path_str:
        return False
    try:
        return Path(path_str).expanduser().is_file()
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Per-provider probes
# ---------------------------------------------------------------------------


def _probe_github() -> CredentialStatus:
    op = _load_operator_config().get("github") or {}
    token_env = op.get("tokenEnv") or "GITHUB_TOKEN"
    token = _resolve_env(token_env)
    if token:
        return CredentialStatus(
            available=True,
            source=f"env:{token_env}",
            env_vars={"GITHUB_PERSONAL_ACCESS_TOKEN": token},
        )
    for fallback in ("GITHUB_PERSONAL_ACCESS_TOKEN", "GH_TOKEN"):
        token = _resolve_env(fallback)
        if token:
            return CredentialStatus(
                available=True,
                source=f"env:{fallback}",
                env_vars={"GITHUB_PERSONAL_ACCESS_TOKEN": token},
            )
    return CredentialStatus(False, "none")


def _probe_gitlab() -> CredentialStatus:
    op = _load_operator_config().get("gitlab") or {}
    token_env = op.get("tokenEnv") or "GITLAB_TOKEN"
    token = _resolve_env(token_env) or _resolve_env("GITLAB_API_TOKEN")
    instance_url = op.get("instanceUrl") or _resolve_env("GITLAB_INSTANCE_URL")
    if not token:
        return CredentialStatus(False, "none")
    env_vars = {"GITLAB_PERSONAL_ACCESS_TOKEN": token}
    if instance_url:
        env_vars["GITLAB_API_URL"] = instance_url
    return CredentialStatus(available=True, source=f"env:{token_env}", env_vars=env_vars)


def _probe_azure_devops() -> CredentialStatus:
    op = _load_operator_config().get("azureDevOps") or {}
    token_env = op.get("tokenEnv") or "AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN"
    token = _resolve_env(token_env)
    org_url = op.get("orgUrl") or _resolve_env("AZURE_DEVOPS_ORG_SERVICE_URL")
    if not token or not org_url:
        return CredentialStatus(False, "none")
    return CredentialStatus(
        available=True,
        source=f"env:{token_env}",
        env_vars={
            "AZURE_DEVOPS_PERSONAL_ACCESS_TOKEN": token,
            "AZURE_DEVOPS_ORG_SERVICE_URL": org_url,
        },
    )


def _probe_kubernetes() -> CredentialStatus:
    # 1. Operator-configured kubeconfig path
    op = _load_operator_config().get("kubernetes") or {}
    kubeconfig_path = op.get("kubeconfigPath")
    if _file_exists(kubeconfig_path):
        return CredentialStatus(
            available=True,
            source=f"file:{kubeconfig_path}",
            env_vars={"KUBECONFIG": str(Path(kubeconfig_path).expanduser())},
        )

    # 2. KUBECONFIG env var
    env_kubeconfig = _resolve_env("KUBECONFIG")
    if env_kubeconfig and any(_file_exists(p) for p in env_kubeconfig.split(":")):
        return CredentialStatus(
            available=True,
            source="env:KUBECONFIG",
            env_vars={"KUBECONFIG": env_kubeconfig},
        )

    # 3. Default ~/.kube/config
    default = Path.home() / ".kube" / "config"
    if default.is_file():
        return CredentialStatus(
            available=True,
            source="file:~/.kube/config",
            env_vars={"KUBECONFIG": str(default)},
        )

    # 4. K8s in-cluster service account
    if K8S_SERVICE_ACCOUNT_TOKEN.is_file():
        return CredentialStatus(available=True, source="k8s-in-cluster")

    return CredentialStatus(False, "none")


def _probe_aws() -> CredentialStatus:
    op = _load_operator_config().get("aws") or {}

    # Operator-configured profile
    profile = op.get("profile")
    if profile:
        creds_file = Path.home() / ".aws" / "credentials"
        if creds_file.is_file():
            return CredentialStatus(
                available=True,
                source=f"profile:{profile}",
                env_vars={"AWS_PROFILE": profile},
            )

    # Explicit access keys via env (most direct)
    if _resolve_env("AWS_ACCESS_KEY_ID") and _resolve_env("AWS_SECRET_ACCESS_KEY"):
        return CredentialStatus(available=True, source="env:AWS_ACCESS_KEY_ID")

    # AWS_PROFILE pointing into ~/.aws/credentials
    env_profile = _resolve_env("AWS_PROFILE")
    creds_file = Path.home() / ".aws" / "credentials"
    if env_profile and creds_file.is_file():
        return CredentialStatus(available=True, source=f"env:AWS_PROFILE={env_profile}")

    # Default ~/.aws/credentials (default profile)
    if creds_file.is_file():
        return CredentialStatus(available=True, source="file:~/.aws/credentials")

    # IRSA — EKS service account web identity (presence of the projected token file is sufficient)
    if _resolve_env("AWS_ROLE_ARN") and _resolve_env("AWS_WEB_IDENTITY_TOKEN_FILE"):
        return CredentialStatus(available=True, source="irsa")

    return CredentialStatus(False, "none")


def _probe_azure() -> CredentialStatus:
    op = _load_operator_config().get("azure") or {}

    # Service principal via operator config (refs env vars; doesn't store secret)
    tenant = op.get("tenantId") or _resolve_env("AZURE_TENANT_ID")
    client = op.get("clientId") or _resolve_env("AZURE_CLIENT_ID")
    secret_env = op.get("clientSecretEnv") or "AZURE_CLIENT_SECRET"
    secret = _resolve_env(secret_env)
    if tenant and client and secret:
        return CredentialStatus(
            available=True,
            source="service-principal",
            env_vars={
                "AZURE_TENANT_ID": tenant,
                "AZURE_CLIENT_ID": client,
                "AZURE_CLIENT_SECRET": secret,
            },
        )

    # Azure CLI cache — `az login` populates ~/.azure/
    az_profile = Path.home() / ".azure" / "azureProfile.json"
    if az_profile.is_file():
        return CredentialStatus(available=True, source="file:~/.azure/azureProfile.json")

    # Managed Identity is host-side and not probe-able without an API call;
    # the MCP server's MSAL layer will discover it at startup. We treat
    # AZURE_USE_MSI=true as the operator's signal that MI is available.
    if _resolve_env("AZURE_USE_MSI") == "true":
        return CredentialStatus(available=True, source="managed-identity")

    return CredentialStatus(False, "none")


def _probe_gcp() -> CredentialStatus:
    op = _load_operator_config().get("gcp") or {}

    creds_path = op.get("credentialsPath") or _resolve_env("GOOGLE_APPLICATION_CREDENTIALS")
    if _file_exists(creds_path):
        return CredentialStatus(
            available=True,
            source=f"file:{creds_path}",
            env_vars={"GOOGLE_APPLICATION_CREDENTIALS": str(Path(creds_path).expanduser())},
        )

    # gcloud ADC default location
    adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if adc.is_file():
        return CredentialStatus(
            available=True,
            source="file:~/.config/gcloud/application_default_credentials.json",
            env_vars={"GOOGLE_APPLICATION_CREDENTIALS": str(adc)},
        )

    return CredentialStatus(False, "none")


def _probe_deployment_metrics() -> CredentialStatus:
    """Credential status for the RFC-0013 deployment-metrics MCP (#644).

    Honest by construction: AIFactory does not ship a runnable deployment-metrics
    MCP server (the hub's ``deployment_metrics_stub.py`` is a reference *library*,
    not a stdio server). The server is therefore available ONLY when the operator
    declares a real one in ~/.aifactory/mcp-servers.json. When declared, its
    ``env`` references are resolved here so the subprocess gets secrets via the
    environment, never argv. Absent that, ``available=False`` — and the contract's
    ``dora_context`` honestly reports unavailable (degrade, never fabricate).
    """
    spec = get_operator_mcp_servers().get("deployment-metrics")
    if not spec:
        return CredentialStatus(False, "none")
    return CredentialStatus(
        available=True,
        source="operator-config",
        env_vars=resolve_operator_mcp_env(spec),
    )


_PROBES: dict[str, callable] = {
    "github": _probe_github,
    "gitlab": _probe_gitlab,
    "azure_devops": _probe_azure_devops,
    "kubernetes": _probe_kubernetes,
    "aws": _probe_aws,
    "azure": _probe_azure,
    "gcp": _probe_gcp,
    "deployment_metrics": _probe_deployment_metrics,
}


def get_credential_status(provider: str) -> CredentialStatus:
    """Return whether credentials for ``provider`` appear available.

    See module docstring for the resolution order. Unknown providers always
    return ``available=False``.
    """
    probe = _PROBES.get(provider)
    if probe is None:
        return CredentialStatus(False, f"unknown-provider:{provider}")
    return probe()


def reset_cache() -> None:
    """Drop the cached operator configs. Test/CLI helper — not used at runtime."""
    _load_operator_config.cache_clear()
    _load_operator_mcp_servers.cache_clear()
