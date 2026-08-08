"""Tests for the KubernetesClient wrapper (Epic #35 #36 PR-2).

Covers:
  - Namespace / ServiceAccount / Role + Binding creation idempotency.
  - 409 AlreadyExists is swallowed; other errors raise
    KubernetesClientError.
  - CNI-backend branching (Calico vs Cilium policy shape).
  - ResourceQuota + LimitRange shapes match design defaults.
  - delete_namespace tolerates 404.
  - get_namespace_status returns phase + RFC3339 deletion timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from server.services.k8s_client import (
    KubernetesClient,
    KubernetesClientError,
)

pytestmark = pytest.mark.tenant_isolation


def _api_exception(status: int, reason: str = "Test"):
    """Build a kubernetes_asyncio.client.rest.ApiException."""
    from kubernetes_asyncio.client.rest import ApiException

    return ApiException(status=status, reason=reason)


@pytest.fixture
def client():
    """Return a KubernetesClient pre-flagged as configured (no auto-config)."""
    c = KubernetesClient()
    c._configured = True
    return c


# ---------------------------------------------------------------------------
# Namespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tenant_namespace_emits_labels(client):
    """The body passed to create_namespace carries the right labels."""
    api = MagicMock()
    api.create_namespace = AsyncMock()
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        await client.create_tenant_namespace(
            "aifactory-tenant-acme",
            {"aifactory.io/tenant": "org-123"},
        )

    api.create_namespace.assert_awaited_once()
    body = api.create_namespace.call_args.kwargs["body"]
    assert body.metadata.name == "aifactory-tenant-acme"
    assert body.metadata.labels == {"aifactory.io/tenant": "org-123"}


@pytest.mark.asyncio
async def test_create_tenant_namespace_swallows_409(client):
    """A 409 AlreadyExists is treated as success (idempotent)."""
    api = MagicMock()
    api.create_namespace = AsyncMock(side_effect=_api_exception(409))
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        # Should NOT raise.
        await client.create_tenant_namespace("foo", {})


@pytest.mark.asyncio
async def test_create_tenant_namespace_raises_on_other_errors(client):
    api = MagicMock()
    api.create_namespace = AsyncMock(side_effect=_api_exception(500))
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        with pytest.raises(KubernetesClientError):
            await client.create_tenant_namespace("foo", {})


@pytest.mark.asyncio
async def test_delete_namespace_tolerates_404(client):
    api = MagicMock()
    api.delete_namespace = AsyncMock(side_effect=_api_exception(404))
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        # No raise — already gone is idempotent success.
        await client.delete_namespace("gone")


@pytest.mark.asyncio
async def test_get_namespace_status_returns_phase_and_timestamp(client):
    """When the SDK returns a Terminating namespace with a
    deletion_timestamp, get_namespace_status surfaces both."""
    ns = MagicMock()
    ns.status.phase = "Terminating"
    ns.metadata.deletion_timestamp = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)

    api = MagicMock()
    api.read_namespace = AsyncMock(return_value=ns)
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        status = await client.get_namespace_status("foo")

    assert status.phase == "Terminating"
    assert status.deletion_timestamp is not None
    assert status.deletion_timestamp.startswith("2026-05-28T12:00:00")


@pytest.mark.asyncio
async def test_get_namespace_status_404_returns_empty_status(client):
    api = MagicMock()
    api.read_namespace = AsyncMock(side_effect=_api_exception(404))
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        status = await client.get_namespace_status("gone")

    assert status.phase == ""
    assert status.deletion_timestamp is None


# ---------------------------------------------------------------------------
# ServiceAccount
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_service_account_annotates_irsa(client):
    """When irsa_role_arn is given, the SA carries the EKS annotation."""
    api = MagicMock()
    api.create_namespaced_service_account = AsyncMock()
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        await client.create_service_account(
            namespace="aifactory-tenant-acme",
            name="aifactory-tenant-acme-agent",
            irsa_role_arn="arn:aws:iam::123:role/aifactory-tenant-uuid",
        )

    body = api.create_namespaced_service_account.call_args.kwargs["body"]
    assert body.metadata.annotations == {
        "eks.amazonaws.com/role-arn": "arn:aws:iam::123:role/aifactory-tenant-uuid",
    }


@pytest.mark.asyncio
async def test_create_service_account_no_irsa_omits_annotation(client):
    api = MagicMock()
    api.create_namespaced_service_account = AsyncMock()
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        await client.create_service_account(
            namespace="ns",
            name="sa",
            irsa_role_arn=None,
        )

    body = api.create_namespaced_service_account.call_args.kwargs["body"]
    assert body.metadata.annotations is None


@pytest.mark.asyncio
async def test_create_service_account_409_patches_annotation(client):
    """On 409 the existing SA gets a PATCH so a late IRSA ARN lands."""
    api = MagicMock()
    api.create_namespaced_service_account = AsyncMock(
        side_effect=_api_exception(409),
    )
    api.patch_namespaced_service_account = AsyncMock()
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        await client.create_service_account(
            namespace="ns",
            name="sa",
            irsa_role_arn="arn:aws:iam::123:role/whatever",
        )

    api.patch_namespaced_service_account.assert_awaited_once()
    patch_body = api.patch_namespaced_service_account.call_args.kwargs["body"]
    assert patch_body["metadata"]["annotations"][
        "eks.amazonaws.com/role-arn"
    ].startswith("arn:aws:iam::123:role/")


# ---------------------------------------------------------------------------
# Role + RoleBinding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_and_binding_minimal_scope(client):
    """The Role grants only pods get/list/watch (no mutations)."""
    api = MagicMock()
    api.create_namespaced_role = AsyncMock()
    api.create_namespaced_role_binding = AsyncMock()
    api.api_client.close = AsyncMock()

    with patch(
        "kubernetes_asyncio.client.RbacAuthorizationV1Api",
        return_value=api,
    ):
        await client.create_role_and_binding("ns", "sa")

    role_body = api.create_namespaced_role.call_args.kwargs["body"]
    assert len(role_body.rules) == 1
    assert role_body.rules[0].resources == ["pods"]
    assert set(role_body.rules[0].verbs) == {"get", "list", "watch"}

    bind_body = api.create_namespaced_role_binding.call_args.kwargs["body"]
    assert bind_body.subjects[0].name == "sa"
    assert bind_body.subjects[0].namespace == "ns"


@pytest.mark.asyncio
async def test_create_role_and_binding_409_is_idempotent(client):
    api = MagicMock()
    api.create_namespaced_role = AsyncMock(side_effect=_api_exception(409))
    api.create_namespaced_role_binding = AsyncMock(
        side_effect=_api_exception(409),
    )
    api.api_client.close = AsyncMock()

    with patch(
        "kubernetes_asyncio.client.RbacAuthorizationV1Api",
        return_value=api,
    ):
        # No raise.
        await client.create_role_and_binding("ns", "sa")


# ---------------------------------------------------------------------------
# NetworkPolicy — CNI branching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_network_policies_cilium_emits_ciliumnetworkpolicy(client):
    """cni_backend=cilium uses the CiliumNetworkPolicy CRD."""
    net_api = MagicMock()
    net_api.create_namespaced_network_policy = AsyncMock()
    net_api.api_client.close = AsyncMock()

    custom_api = MagicMock()
    custom_api.create_namespaced_custom_object = AsyncMock()
    custom_api.api_client.close = AsyncMock()

    with (
        patch(
            "kubernetes_asyncio.client.NetworkingV1Api",
            return_value=net_api,
        ),
        patch(
            "kubernetes_asyncio.client.CustomObjectsApi",
            return_value=custom_api,
        ),
    ):
        await client.apply_network_policies(
            namespace="ns",
            allowed_fqdns=["api.anthropic.com"],
            cni_backend="cilium",
        )

    # default-deny is applied via NetworkingV1Api.
    net_api.create_namespaced_network_policy.assert_awaited_once()
    # Egress allow-list is applied via the Cilium CRD.
    custom_api.create_namespaced_custom_object.assert_awaited_once()
    cr = custom_api.create_namespaced_custom_object.call_args.kwargs
    assert cr["group"] == "cilium.io"
    assert cr["plural"] == "ciliumnetworkpolicies"
    spec = cr["body"]["spec"]
    fqdns = [e["matchName"] for e in spec["egress"][0]["toFQDNs"]]
    assert fqdns == ["api.anthropic.com"]


@pytest.mark.asyncio
async def test_apply_network_policies_calico_emits_standard_networkpolicy(
    client,
):
    """cni_backend=calico uses the upstream NetworkPolicy with FQDN
    annotation (the Helm chart materialises GlobalNetworkSets)."""
    net_api = MagicMock()
    net_api.create_namespaced_network_policy = AsyncMock()
    net_api.api_client.close = AsyncMock()

    with patch(
        "kubernetes_asyncio.client.NetworkingV1Api",
        return_value=net_api,
    ):
        await client.apply_network_policies(
            namespace="ns",
            allowed_fqdns=["api.anthropic.com", "auth.example.com"],
            cni_backend="calico",
        )

    # Two calls: default-deny + allow-egress-known-destinations.
    assert net_api.create_namespaced_network_policy.await_count == 2
    bodies = [
        c.kwargs["body"]
        for c in net_api.create_namespaced_network_policy.call_args_list
    ]
    allow = [b for b in bodies if b.metadata.name.startswith("allow-")][0]
    assert (
        allow.metadata.annotations["aifactory.io/allowed-fqdns"]
        == "api.anthropic.com,auth.example.com"
    )


@pytest.mark.asyncio
async def test_apply_network_policies_auto_falls_back_to_calico(client):
    """auto mode probes for Cilium CRD; absent → Calico shape."""
    # No Cilium CRD in the cluster — the probe iterates ``crds.items``
    # and reads ``crd.spec.group`` on each.
    fake_crd = MagicMock()
    fake_crd.spec.group = "something-else.io"
    crds = MagicMock()
    crds.items = [fake_crd]

    apiext_api = MagicMock()
    apiext_api.list_custom_resource_definition = AsyncMock(return_value=crds)
    apiext_api.api_client.close = AsyncMock()

    net_api = MagicMock()
    net_api.create_namespaced_network_policy = AsyncMock()
    net_api.api_client.close = AsyncMock()

    with (
        patch(
            "kubernetes_asyncio.client.ApiextensionsV1Api",
            return_value=apiext_api,
        ),
        patch(
            "kubernetes_asyncio.client.NetworkingV1Api",
            return_value=net_api,
        ),
    ):
        await client.apply_network_policies(
            namespace="ns",
            allowed_fqdns=["x"],
            cni_backend="auto",
        )

    # default-deny + calico allow = 2 standard NetworkPolicy creates.
    assert net_api.create_namespaced_network_policy.await_count == 2


@pytest.mark.asyncio
async def test_apply_network_policies_unsupported_backend_raises(client):
    with pytest.raises(KubernetesClientError):
        await client.apply_network_policies(
            namespace="ns",
            allowed_fqdns=[],
            cni_backend="weave",
        )


# ---------------------------------------------------------------------------
# ResourceQuota + LimitRange
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_resource_quota_emits_pod_pvc_caps(client):
    api = MagicMock()
    api.create_namespaced_resource_quota = AsyncMock()
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        await client.apply_resource_quota("ns", pod_count=42, pvc_count=7)

    body = api.create_namespaced_resource_quota.call_args.kwargs["body"]
    assert body.spec.hard == {
        "pods": "42",
        "persistentvolumeclaims": "7",
    }


@pytest.mark.asyncio
async def test_apply_limit_range_emits_container_defaults(client):
    api = MagicMock()
    api.create_namespaced_limit_range = AsyncMock()
    api.api_client.close = AsyncMock()

    with patch("kubernetes_asyncio.client.CoreV1Api", return_value=api):
        await client.apply_limit_range("ns", "500m", "512Mi")

    body = api.create_namespaced_limit_range.call_args.kwargs["body"]
    item = body.spec.limits[0]
    assert item.type == "Container"
    assert item.default == {"cpu": "500m", "memory": "512Mi"}
    assert item.default_request == {"cpu": "500m", "memory": "512Mi"}
