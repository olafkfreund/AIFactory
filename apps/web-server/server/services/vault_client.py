"""Vault wrapper for tenant reconciler (Epic #35 #36 PR-2).

What this ships
---------------

Per-tenant Vault policy + Kubernetes-auth role provisioning. The
reconciler asks Vault to:

  1. Write a policy that grants read+list on ``aifactory/data/orgs/<uuid>/*``.
  2. Bind that policy to a Kubernetes auth role keyed on the tenant
     namespace + ServiceAccount.

The web pod authenticates to Vault via the K8s auth method using its
own SA token. The dedicated ``aifactory-reconciler`` AppRole (which
the operator pre-creates via the documented Terraform module) is what
limits the web pod's blast radius — see design §5 reviewer finding #3.

Failure-safe contract
---------------------

Every call wraps in try/except and raises ``VaultClientError`` so the
reconciler records the message in ``tenant_states.reconcile_error``
and retries next tick. If Vault is unreachable entirely the reconciler
SKIPS the Vault steps + logs WARNING — the K8s/IAM resources still
land (a partial reconcile is better than refusing to run).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The SA-token path inside the web pod. Standard K8s convention; the
# kubelet mounts it automatically when the pod has a SA assigned.
_SA_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")


class VaultClientError(RuntimeError):
    """Raised when a Vault API call fails or Vault is misconfigured."""


def _policy_name(org_uuid: str) -> str:
    return f"aifactory-tenant-{org_uuid}"


def _kubernetes_role_name(org_uuid: str) -> str:
    return f"aifactory-tenant-{org_uuid}"


def build_tenant_policy_hcl(org_uuid: str) -> str:
    """Render the HCL policy text for one tenant (design §5).

    Module-level helper so tests can assert the exact text shape
    without instantiating the client.
    """
    # WHY: ``aifactory/data/...`` is the KV-v2 data path; the metadata
    # path is intentionally omitted so tenants can read secret values
    # but not enumerate / introspect version history.
    return (
        f'path "aifactory/data/orgs/{org_uuid}/*" {{\n'
        f'  capabilities = ["read", "list"]\n'
        f"}}\n"
    )


class VaultClient:
    """``hvac``-backed Vault wrapper for the tenant reconciler.

    Construction is cheap — the hvac client is only created on first
    use. The K8s-auth login happens lazily too so unit tests that
    mock hvac don't need to fake the token file.
    """

    def __init__(
        self,
        *,
        addr: str | None = None,
        kubernetes_role: str = "aifactory-reconciler",
        kubernetes_auth_mount: str = "kubernetes",
    ) -> None:
        # Resolve at construction time so misconfigured envs fail
        # earlier than first use. Empty addr → caller's reconciler
        # decides to skip the Vault step entirely (Vault-disabled
        # deployment).
        self._addr = addr or os.environ.get("VAULT_ADDR") or ""
        self._role = kubernetes_role
        self._mount = kubernetes_auth_mount
        self._client: Any = None
        self._authenticated = False

    # -- Lazy client + auth --------------------------------------------

    def _hvac_client(self) -> Any:
        if self._client is None:
            if not self._addr:
                raise VaultClientError(
                    "VAULT_ADDR is not set; refusing to create Vault client",
                )
            import hvac
            self._client = hvac.Client(url=self._addr)
        return self._client

    def _authenticate(self) -> None:
        """Log in via the K8s auth method using the web pod's SA token.

        Idempotent — once authenticated the hvac client holds the
        Vault token internally so subsequent calls reuse it.
        """
        if self._authenticated:
            return
        client = self._hvac_client()

        try:
            sa_token = _SA_TOKEN_PATH.read_text().strip()
        except FileNotFoundError as exc:
            raise VaultClientError(
                f"K8s SA token not found at {_SA_TOKEN_PATH}; the web pod "
                f"must run with a mounted ServiceAccount to authenticate "
                f"to Vault via the kubernetes auth method",
            ) from exc

        try:
            client.auth.kubernetes.login(
                role=self._role, jwt=sa_token, mount_point=self._mount,
            )
        except Exception as exc:
            raise VaultClientError(
                f"vault kubernetes-auth login failed (role={self._role!r}, "
                f"mount={self._mount!r}): {exc}",
            ) from exc
        self._authenticated = True

    # -- Policy + role lifecycle ---------------------------------------

    def create_tenant_policy(self, org_uuid: str) -> str:
        """Write the per-tenant policy + return its name. Idempotent."""
        self._authenticate()
        client = self._hvac_client()
        name = _policy_name(org_uuid)
        try:
            client.sys.create_or_update_policy(
                name=name, policy=build_tenant_policy_hcl(org_uuid),
            )
        except Exception as exc:
            raise VaultClientError(
                f"create_tenant_policy({org_uuid}) failed: {exc}",
            ) from exc
        return name

    def create_tenant_kubernetes_role(
        self, *, org_uuid: str, sa_namespace: str,
        sa_name: str, policy_name: str,
    ) -> None:
        """Bind a K8s-auth role: tenant SA → tenant policy.

        Once this lands an agent pod in ``sa_namespace`` running as
        ``sa_name`` can authenticate to Vault and the resulting token
        is scoped exactly to ``policy_name``.
        """
        self._authenticate()
        client = self._hvac_client()
        role_name = _kubernetes_role_name(org_uuid)
        try:
            client.auth.kubernetes.create_role(
                name=role_name,
                bound_service_account_names=[sa_name],
                bound_service_account_namespaces=[sa_namespace],
                policies=[policy_name],
                ttl="1h",
                mount_point=self._mount,
            )
        except Exception as exc:
            raise VaultClientError(
                f"create_tenant_kubernetes_role({org_uuid}) failed: {exc}",
            ) from exc

    def delete_tenant_policy_and_role(self, org_uuid: str) -> None:
        """Tear down the policy + K8s-auth role for a torn-down tenant.

        Each step is independent and idempotent — a missing policy or
        role is treated as success.
        """
        self._authenticate()
        client = self._hvac_client()
        role_name = _kubernetes_role_name(org_uuid)
        policy_name = _policy_name(org_uuid)

        try:
            client.auth.kubernetes.delete_role(
                name=role_name, mount_point=self._mount,
            )
        except Exception:
            # hvac raises ``InvalidPath`` for already-deleted roles;
            # we treat any error here as best-effort.
            logger.debug(
                "vault delete kubernetes role %s failed (likely already gone)",
                role_name, exc_info=True,
            )

        try:
            client.sys.delete_policy(name=policy_name)
        except Exception:
            logger.debug(
                "vault delete policy %s failed (likely already gone)",
                policy_name, exc_info=True,
            )
