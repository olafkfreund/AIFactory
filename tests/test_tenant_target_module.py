"""Tenant namespace routing lives in services/tenant_target.py.

Contract: standalone import (no agent_service load) + agent_service re-exports
the SAME objects so existing callers are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))


def test_tenant_target_self_contained():
    from server.services import tenant_target

    assert hasattr(tenant_target, "TenantTarget")
    assert hasattr(tenant_target, "resolve_tenant_target")


def test_agent_service_reexports_same_objects():
    from server.services import agent_service, tenant_target

    assert agent_service.TenantTarget is tenant_target.TenantTarget
    assert agent_service.resolve_tenant_target is tenant_target.resolve_tenant_target
