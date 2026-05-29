"""Kubernetes wrapper for the tenant reconciler (Epic #35 #36 PR-2).

What this ships
---------------

A narrow async facade over ``kubernetes-asyncio`` so the reconciler
can create + delete per-tenant Namespace / ServiceAccount / Role /
RoleBinding / NetworkPolicy / ResourceQuota / LimitRange resources.

Every public method is idempotent on the K8s side: ``AlreadyExists``
(409) is treated as success so a re-running reconcile pass doesn't
flap. Every method raises ``KubernetesClientError`` on real failure
so the reconciler can record the message into
``tenant_states.reconcile_error`` and retry next tick (failure-safe
contract — see ``tenant_reconciler.py``).

CNI capability lock (design decision #3)
----------------------------------------

FQDN-based egress policies require Calico or Cilium. The reconciler
picks the policy shape via ``cni_backend`` ∈ {``calico``, ``cilium``,
``auto``}. ``auto`` first tries Cilium (CRD ``CiliumNetworkPolicy``)
and falls back to Calico's ``NetworkPolicy`` with FQDN rules if the
Cilium CRD is absent. Operators see the Helm pre-install check from
PR-3 long before this code runs in production; the runtime check
here is the second line of defence.

Lazy SDK import
---------------

``kubernetes-asyncio`` is only imported on first use so non-isolated
deployments don't pay the cold-start cost. Same pattern as the KMS
backends (see ``crypto/kms/aws.py``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions + helpers
# ---------------------------------------------------------------------------


class KubernetesClientError(RuntimeError):
    """Raised when the K8s API call fails for any reason other than 409.

    The reconciler catches this, records the message into
    ``tenant_states.reconcile_error``, and retries next tick.
    """


@dataclass(frozen=True)
class NamespaceStatus:
    """Subset of ``V1Namespace.status`` the reconciler cares about.

    ``deletion_timestamp`` is the operator-facing way to detect a
    stuck-Terminating namespace (design §7 stage-2). The reconciler
    compares it to ``now`` and alerts when the gap exceeds 30 min.
    """

    phase: str  # 'Active' | 'Terminating' | ''
    deletion_timestamp: str | None  # RFC3339 string from the API


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class KubernetesClient:
    """Async wrapper over ``kubernetes-asyncio``.

    The constructor takes no args — config is loaded from the env on
    first use (in-cluster ServiceAccount or ``KUBECONFIG``).
    """

    # CRD/API group constants — module-level so tests can assert on them.
    CILIUM_API_GROUP = "cilium.io"
    CALICO_API_GROUP = "crd.projectcalico.org"

    def __init__(self) -> None:
        self._configured = False

    # -- Lazy SDK + config ---------------------------------------------

    async def _ensure_configured(self) -> None:
        """Load K8s config exactly once.

        In-cluster (web pod's mounted SA token) is the production
        path. ``KUBECONFIG`` is the dev / minikube path.
        """
        if self._configured:
            return
        from kubernetes_asyncio import config as k8s_config

        try:
            # In-cluster takes precedence — looks for the SA token
            # mount; raises ConfigException when not present.
            k8s_config.load_incluster_config()
        except Exception:
            # Fall back to kubeconfig (dev / minikube).
            try:
                await k8s_config.load_kube_config(
                    config_file=os.environ.get("KUBECONFIG"),
                )
            except Exception as exc:
                raise KubernetesClientError(
                    f"K8s config unavailable (no in-cluster, no KUBECONFIG): {exc}",
                ) from exc
        self._configured = True

    # -- Namespace -----------------------------------------------------

    async def create_tenant_namespace(
        self, name: str, labels: dict[str, str],
    ) -> None:
        """Create a Namespace, swallowing 409 AlreadyExists."""
        await self._ensure_configured()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        body = k8s_client.V1Namespace(
            metadata=k8s_client.V1ObjectMeta(name=name, labels=labels),
        )
        api = k8s_client.CoreV1Api()
        try:
            await api.create_namespace(body=body)
        except ApiException as exc:
            if exc.status == 409:
                logger.debug("namespace %s already exists; idempotent", name)
                return
            raise KubernetesClientError(
                f"create_namespace({name}) failed: {exc.status} {exc.reason}",
            ) from exc
        finally:
            await api.api_client.close()

    async def delete_namespace(self, name: str) -> None:
        """Async-delete a Namespace; returns immediately.

        K8s does the cascade via finalizers; the reconciler's job is
        to detect stuck-Terminating via ``get_namespace_status``.
        """
        await self._ensure_configured()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        api = k8s_client.CoreV1Api()
        try:
            await api.delete_namespace(name=name)
        except ApiException as exc:
            if exc.status == 404:
                return  # already gone — idempotent
            raise KubernetesClientError(
                f"delete_namespace({name}) failed: {exc.status} {exc.reason}",
            ) from exc
        finally:
            await api.api_client.close()

    async def get_namespace_status(self, name: str) -> NamespaceStatus:
        """Fetch phase + deletionTimestamp for stuck-Terminating detection."""
        await self._ensure_configured()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        api = k8s_client.CoreV1Api()
        try:
            ns = await api.read_namespace(name=name)
        except ApiException as exc:
            if exc.status == 404:
                # Caller treats missing ns as "already gone"; not an error.
                return NamespaceStatus(phase="", deletion_timestamp=None)
            raise KubernetesClientError(
                f"read_namespace({name}) failed: {exc.status} {exc.reason}",
            ) from exc
        finally:
            await api.api_client.close()

        phase = (ns.status.phase if ns.status else "") or ""
        # ``deletion_timestamp`` is a datetime on the SDK object; stringify
        # so the reconciler can keep it simple (consistent JSON-able shape).
        dt = ns.metadata.deletion_timestamp if ns.metadata else None
        return NamespaceStatus(
            phase=phase,
            deletion_timestamp=(dt.isoformat() if dt else None),
        )

    # -- ServiceAccount ------------------------------------------------

    async def create_service_account(
        self, namespace: str, name: str, irsa_role_arn: str | None,
    ) -> None:
        """Create a ServiceAccount; annotate with IRSA role ARN when supplied.

        ``eks.amazonaws.com/role-arn`` is the EKS-side IRSA convention; on
        non-AWS clusters the annotation is harmless (ignored by the kubelet).
        """
        await self._ensure_configured()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        annotations: dict[str, str] = {}
        if irsa_role_arn:
            annotations["eks.amazonaws.com/role-arn"] = irsa_role_arn

        body = k8s_client.V1ServiceAccount(
            metadata=k8s_client.V1ObjectMeta(
                name=name, namespace=namespace,
                annotations=annotations or None,
            ),
        )
        api = k8s_client.CoreV1Api()
        try:
            await api.create_namespaced_service_account(
                namespace=namespace, body=body,
            )
        except ApiException as exc:
            if exc.status == 409:
                # Patch annotations if the SA already exists — handles
                # the case where the SA was created without IRSA the
                # first pass + the IAM role lands on a later pass.
                if annotations:
                    try:
                        patch = {"metadata": {"annotations": annotations}}
                        await api.patch_namespaced_service_account(
                            name=name, namespace=namespace, body=patch,
                        )
                    except ApiException as patch_exc:
                        raise KubernetesClientError(
                            f"patch SA {namespace}/{name} failed: "
                            f"{patch_exc.status} {patch_exc.reason}",
                        ) from patch_exc
                return
            raise KubernetesClientError(
                f"create_service_account({namespace}/{name}) failed: "
                f"{exc.status} {exc.reason}",
            ) from exc
        finally:
            await api.api_client.close()

    # -- Role + RoleBinding --------------------------------------------

    async def create_role_and_binding(
        self, namespace: str, sa_name: str,
    ) -> None:
        """Create a minimal Role + RoleBinding granting pod read in the ns.

        The Role grants only ``pods: get/list/watch`` — enough for the
        agent pod to introspect its own namespace (helpful for diagnostic
        commands like ``kubectl get pods``) but not enough to mutate
        anything or peek into other namespaces.
        """
        await self._ensure_configured()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        role_name = f"{sa_name}-role"
        binding_name = f"{sa_name}-binding"

        role = k8s_client.V1Role(
            metadata=k8s_client.V1ObjectMeta(
                name=role_name, namespace=namespace,
            ),
            rules=[
                k8s_client.V1PolicyRule(
                    api_groups=[""],
                    resources=["pods"],
                    verbs=["get", "list", "watch"],
                ),
            ],
        )
        binding = k8s_client.V1RoleBinding(
            metadata=k8s_client.V1ObjectMeta(
                name=binding_name, namespace=namespace,
            ),
            role_ref=k8s_client.V1RoleRef(
                api_group="rbac.authorization.k8s.io",
                kind="Role",
                name=role_name,
            ),
            subjects=[
                k8s_client.RbacV1Subject(
                    kind="ServiceAccount",
                    name=sa_name,
                    namespace=namespace,
                ),
            ],
        )

        rbac_api = k8s_client.RbacAuthorizationV1Api()
        try:
            try:
                await rbac_api.create_namespaced_role(
                    namespace=namespace, body=role,
                )
            except ApiException as exc:
                if exc.status != 409:
                    raise KubernetesClientError(
                        f"create_role({namespace}/{role_name}) failed: "
                        f"{exc.status} {exc.reason}",
                    ) from exc

            try:
                await rbac_api.create_namespaced_role_binding(
                    namespace=namespace, body=binding,
                )
            except ApiException as exc:
                if exc.status != 409:
                    raise KubernetesClientError(
                        f"create_role_binding({namespace}/{binding_name}) "
                        f"failed: {exc.status} {exc.reason}",
                    ) from exc
        finally:
            await rbac_api.api_client.close()

    # -- NetworkPolicy -------------------------------------------------

    async def apply_network_policies(
        self, namespace: str, allowed_fqdns: list[str], cni_backend: str,
    ) -> None:
        """Emit default-deny + allow-egress policies.

        ``cni_backend`` selects the CRD shape:
          - ``cilium``  → ``CiliumNetworkPolicy`` with toFQDNs rules
          - ``calico``  → upstream ``NetworkPolicy`` plus Calico FQDN
                          ``GlobalNetworkSet`` annotations
          - ``auto``    → try Cilium first; fall back to Calico when
                          the Cilium CRD is missing

        Default-deny ingress also lands so cross-tenant pod-to-pod
        traffic is blocked.
        """
        await self._ensure_configured()

        # WHY: validate the backend BEFORE touching the API so an
        # unsupported value fails fast without leaving a half-applied
        # default-deny in place.
        if cni_backend not in ("cilium", "calico", "auto"):
            raise KubernetesClientError(
                f"unsupported cni_backend={cni_backend!r}; expected "
                f"calico|cilium|auto",
            )

        chosen = cni_backend
        if cni_backend == "auto":
            chosen = (
                "cilium"
                if await self._cilium_crd_available()
                else "calico"
            )

        await self._apply_default_deny(namespace)
        if chosen == "cilium":
            await self._apply_cilium_egress(namespace, allowed_fqdns)
        else:  # calico
            await self._apply_calico_egress(namespace, allowed_fqdns)

    async def _cilium_crd_available(self) -> bool:
        """Cheap CRD-presence probe for the Cilium API group."""
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        api = k8s_client.ApiextensionsV1Api()
        try:
            crds = await api.list_custom_resource_definition()
            for crd in crds.items:
                if (crd.spec.group or "") == self.CILIUM_API_GROUP:
                    return True
            return False
        except ApiException:
            return False
        finally:
            await api.api_client.close()

    async def _apply_default_deny(self, namespace: str) -> None:
        """Default-deny both directions for the namespace."""
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        body = k8s_client.V1NetworkPolicy(
            metadata=k8s_client.V1ObjectMeta(
                name="default-deny", namespace=namespace,
            ),
            spec=k8s_client.V1NetworkPolicySpec(
                pod_selector=k8s_client.V1LabelSelector(match_labels={}),
                policy_types=["Ingress", "Egress"],
            ),
        )
        api = k8s_client.NetworkingV1Api()
        try:
            await api.create_namespaced_network_policy(
                namespace=namespace, body=body,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise KubernetesClientError(
                    f"create default-deny in {namespace}: "
                    f"{exc.status} {exc.reason}",
                ) from exc
        finally:
            await api.api_client.close()

    async def _apply_cilium_egress(
        self, namespace: str, allowed_fqdns: list[str],
    ) -> None:
        """Apply a CiliumNetworkPolicy CR with toFQDNs egress allowlist."""
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        spec: dict[str, Any] = {
            "endpointSelector": {},
            "egress": [
                {
                    "toFQDNs": [{"matchName": fqdn} for fqdn in allowed_fqdns],
                    "toPorts": [
                        {"ports": [{"port": "443", "protocol": "TCP"}]},
                    ],
                },
                # kube-dns always needed so the FQDN resolver itself
                # can reach the cluster DNS service.
                {
                    "toEndpoints": [
                        {"matchLabels": {"k8s:io.kubernetes.pod.namespace":
                                         "kube-system",
                                         "k8s-app": "kube-dns"}},
                    ],
                    "toPorts": [
                        {"ports": [{"port": "53", "protocol": "UDP"}]},
                        {"ports": [{"port": "53", "protocol": "TCP"}]},
                    ],
                },
            ],
        }
        body = {
            "apiVersion": "cilium.io/v2",
            "kind": "CiliumNetworkPolicy",
            "metadata": {
                "name": "allow-egress-known-destinations",
                "namespace": namespace,
            },
            "spec": spec,
        }

        api = k8s_client.CustomObjectsApi()
        try:
            await api.create_namespaced_custom_object(
                group="cilium.io", version="v2", namespace=namespace,
                plural="ciliumnetworkpolicies", body=body,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise KubernetesClientError(
                    f"create cilium NetworkPolicy in {namespace}: "
                    f"{exc.status} {exc.reason}",
                ) from exc
        finally:
            await api.api_client.close()

    async def _apply_calico_egress(
        self, namespace: str, allowed_fqdns: list[str],
    ) -> None:
        """Apply a Calico-flavoured NetworkPolicy.

        Calico's FQDN support uses ``GlobalNetworkSet`` resources + an
        annotation-driven match; the policy itself is the upstream
        ``networking.k8s.io/v1`` shape. We emit a coarser allow-egress
        policy that permits TCP/443 + UDP/53 anywhere — Calico's actual
        FQDN narrowing requires the operator to pre-create the
        ``GlobalNetworkSet`` (the Helm chart in PR-3 ships those).
        """
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        # WHY: Calico FQDN narrowing is an annotation on top of a
        # standard NetworkPolicy — the ``allowed_fqdns`` list is
        # recorded so the operator's GlobalNetworkSet generator (PR-3)
        # can pick it up; the runtime allow rule is the coarse port-
        # only egress.
        annotations = {"aifactory.io/allowed-fqdns": ",".join(allowed_fqdns)}
        body = k8s_client.V1NetworkPolicy(
            metadata=k8s_client.V1ObjectMeta(
                name="allow-egress-known-destinations",
                namespace=namespace,
                annotations=annotations,
            ),
            spec=k8s_client.V1NetworkPolicySpec(
                pod_selector=k8s_client.V1LabelSelector(match_labels={}),
                policy_types=["Egress"],
                egress=[
                    k8s_client.V1NetworkPolicyEgressRule(
                        ports=[
                            k8s_client.V1NetworkPolicyPort(
                                port=443, protocol="TCP",
                            ),
                            k8s_client.V1NetworkPolicyPort(
                                port=53, protocol="UDP",
                            ),
                            k8s_client.V1NetworkPolicyPort(
                                port=53, protocol="TCP",
                            ),
                        ],
                    ),
                ],
            ),
        )
        api = k8s_client.NetworkingV1Api()
        try:
            await api.create_namespaced_network_policy(
                namespace=namespace, body=body,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise KubernetesClientError(
                    f"create calico NetworkPolicy in {namespace}: "
                    f"{exc.status} {exc.reason}",
                ) from exc
        finally:
            await api.api_client.close()

    # -- ResourceQuota + LimitRange ------------------------------------

    async def apply_resource_quota(
        self, namespace: str, pod_count: int, pvc_count: int,
    ) -> None:
        """Cap the namespace's pod + PVC count to prevent noisy neighbours."""
        await self._ensure_configured()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        body = k8s_client.V1ResourceQuota(
            metadata=k8s_client.V1ObjectMeta(
                name="tenant-quota", namespace=namespace,
            ),
            spec=k8s_client.V1ResourceQuotaSpec(
                hard={
                    "pods": str(pod_count),
                    "persistentvolumeclaims": str(pvc_count),
                },
            ),
        )
        api = k8s_client.CoreV1Api()
        try:
            await api.create_namespaced_resource_quota(
                namespace=namespace, body=body,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise KubernetesClientError(
                    f"create_resource_quota({namespace}) failed: "
                    f"{exc.status} {exc.reason}",
                ) from exc
        finally:
            await api.api_client.close()

    async def apply_limit_range(
        self, namespace: str, default_cpu: str, default_memory: str,
    ) -> None:
        """Set per-Container default CPU + memory limits in the namespace."""
        await self._ensure_configured()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        body = k8s_client.V1LimitRange(
            metadata=k8s_client.V1ObjectMeta(
                name="tenant-limits", namespace=namespace,
            ),
            spec=k8s_client.V1LimitRangeSpec(
                limits=[
                    k8s_client.V1LimitRangeItem(
                        type="Container",
                        default={"cpu": default_cpu, "memory": default_memory},
                        default_request={
                            "cpu": default_cpu, "memory": default_memory,
                        },
                    ),
                ],
            ),
        )
        api = k8s_client.CoreV1Api()
        try:
            await api.create_namespaced_limit_range(
                namespace=namespace, body=body,
            )
        except ApiException as exc:
            if exc.status != 409:
                raise KubernetesClientError(
                    f"create_limit_range({namespace}) failed: "
                    f"{exc.status} {exc.reason}",
                ) from exc
        finally:
            await api.api_client.close()
