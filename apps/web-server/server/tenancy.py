"""Per-request tenant resolution + spec stamping (#925, factory-gitops#13/#14).

Off by default: unless ``AIFACTORY_MULTI_TENANT`` is truthy, every request
resolves to the single ``"default"`` tenant and behavior is byte-identical to
single-tenant AIFactory. When the flag is on, the ingress/oauth2-proxy stamps
the caller's tenant on requests as an ``X-Tenant-Id`` header (Keycloak group ->
tenant claim, factory-gitops#13); creation endpoints stamp it into the spec's
``task_metadata.json`` and list endpoints filter by it (a spec with no stamp
belongs to ``"default"``). Mirrors CFactory's ``CFACTORY_MULTI_TENANT`` seam.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from factory_common.logsafe import sanitize_log

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "default"
TENANT_HEADER = "X-Tenant-Id"

# The deployment-wide default org (#319). Defined here rather than in
# ``database.engine`` because ``project_registry.save_projects`` stamps it onto
# unowned registry entries, and the registry must not depend on the database
# layer -- ``engine._backfill_project_orgs`` calls INTO the registry, so the
# reverse edge would be an import cycle. ``database.engine`` re-exports the name
# so every existing ``from ..database.engine import DEFAULT_ORG_ID`` still works.
DEFAULT_ORG_ID = "default"


def multi_tenant_enabled() -> bool:
    """Whether per-request tenant scoping is on (``AIFACTORY_MULTI_TENANT``)."""
    return (os.environ.get("AIFACTORY_MULTI_TENANT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_tenant(request: Any = None) -> str:
    """Tenant for this request: the ``X-Tenant-Id`` header when multi-tenant
    mode is on, else ``"default"``.

    ``request`` is anything with a ``.headers`` mapping (a FastAPI ``Request``);
    ``None`` (direct/test callers) resolves to the default tenant.
    """
    if request is None or not multi_tenant_enabled():
        return DEFAULT_TENANT
    try:
        return (request.headers.get(TENANT_HEADER) or "").strip() or DEFAULT_TENANT
    except AttributeError:
        return DEFAULT_TENANT


# Returned by :func:`spec_tenant` when ``task_metadata.json`` exists but could
# not be read or parsed (permission error, I/O error, corrupt JSON) -- fixed
# fleet-wide, factory-gitops#13/#14-adjacent finding, generalised from #1554
# finding 1: this is NOT the same as "no stamp". A missing file is genuine
# absence and safely defaults to DEFAULT_TENANT (every caller already treats
# an unstamped spec that way); a file that exists but errors tells us nothing
# about which tenant owns it, and every ``spec_tenant(d) == tenant`` filter in
# this codebase (routes/tasks.py, routes/projects.py, services/merger.py) is
# a SECURITY BOUNDARY -- collapsing "unknown" into "default" let an
# unreadable-but-real stamp be read, or written to, by whoever the caller
# resolves to as "default". This sentinel can never equal a real tenant id
# (``resolve_tenant`` only ever returns the ``X-Tenant-Id`` header value or
# ``DEFAULT_TENANT``), so it fails every existing filter closed -- excluded
# from every tenant's view, including default's -- with NO changes needed at
# any of those three call sites.
UNREADABLE_TENANT = "__tenant_unreadable__"


def read_spec_tenant(spec_dir: Path) -> str | None:
    """The tenant stamped on a spec's ``task_metadata.json``.

    ``None`` means genuinely unstamped (no file, or no ``tenant_id`` key).
    An unreadable/corrupt file (exists but errors) returns
    :data:`UNREADABLE_TENANT` instead of ``None`` -- see that constant's
    comment for why conflating the two is the bug.
    """
    try:
        text = (Path(spec_dir) / "task_metadata.json").read_text()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "tenant stamp unreadable for %s (failing closed, not defaulting)",
            sanitize_log(spec_dir),
        )
        return UNREADABLE_TENANT
    try:
        meta = json.loads(text)
    except ValueError:
        logger.warning(
            "tenant stamp unparseable for %s (failing closed, not defaulting)",
            sanitize_log(spec_dir),
        )
        return UNREADABLE_TENANT
    tenant = meta.get("tenant_id")
    return str(tenant) if tenant else None


def spec_tenant(spec_dir: Path) -> str:
    """Like :func:`read_spec_tenant` but a missing stamp means ``"default"``.

    A genuinely unreadable stamp (file exists, read/parse failed) is NEVER
    defaulted -- ``read_spec_tenant`` returns :data:`UNREADABLE_TENANT` for
    that case, a non-empty string that survives the ``or DEFAULT_TENANT``
    fallback below unchanged (only ``None`` -- a real absence -- falls
    through to it), so it fails closed out of every ``== tenant`` filter
    rather than silently resolving to ``"default"`` (#1554 finding 1: this
    used to let an unreadable-but-real stamp be swept as if it belonged to
    whichever tenant a caller resolved to as default).
    """
    return read_spec_tenant(spec_dir) or DEFAULT_TENANT


def stamp_spec_tenant(spec_dir: Path, tenant: str) -> None:
    """Merge ``tenant_id`` into the spec's ``task_metadata.json``.

    No-op unless multi-tenant mode is on, so flag-off deployments keep their
    on-disk artifacts unchanged (missing stamp == default tenant). Best-effort:
    never raises.
    """
    if not multi_tenant_enabled():
        return
    tm_file = Path(spec_dir) / "task_metadata.json"
    try:
        meta: dict = {}
        if tm_file.exists():
            meta = json.loads(tm_file.read_text())
        meta["tenant_id"] = tenant or DEFAULT_TENANT
        tm_file.write_text(json.dumps(meta, indent=2))
    except (OSError, ValueError):
        logger.debug(
            "tenant stamp skipped for %s (best-effort)", sanitize_log(spec_dir)
        )
