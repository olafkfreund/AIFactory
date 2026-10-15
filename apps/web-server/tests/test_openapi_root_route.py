"""The OpenAPI spec must not depend on whether apps/web-server/static/ exists (#1731)."""

import pytest


def test_placeholder_root_route_is_not_in_openapi_spec():
    """Without static/, main.py registers a placeholder ``GET /``; with it, the SPA
    mount owns ``/``. Only the placeholder showed up in the spec, so the committed
    openapi.yaml changed with build output."""
    main = pytest.importorskip("server.main")

    assert "/" not in main.create_app().openapi()["paths"]
