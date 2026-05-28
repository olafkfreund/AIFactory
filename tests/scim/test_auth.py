"""Tests for the SCIM Bearer-token middleware (Epic #35 #41)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

_WEB_SERVER = Path(__file__).parent.parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.scim.auth import require_scim_bearer


def _make_app() -> FastAPI:
    """Tiny app exposing one protected route — keeps the middleware
    test isolated from the full server build."""
    app = FastAPI()

    @app.get("/scim/v2/Probe")
    async def probe(_=Depends(require_scim_bearer)):
        return {"ok": True}

    return app


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    """Each test sets / unsets SCIM_BEARER_TOKEN explicitly."""
    monkeypatch.delenv("SCIM_BEARER_TOKEN", raising=False)
    yield


def test_503_when_token_env_unset():
    """Endpoint exposed but token not configured → 503 (loud), NOT
    a 401 that silently lets through an empty token."""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get(
            "/scim/v2/Probe",
            headers={"Authorization": "Bearer anything"},
        )
    assert resp.status_code == 503
    body = resp.json()
    # FastAPI wraps the HTTPException.detail in {"detail": ...}
    assert "not configured" in body["detail"]["detail"]


def test_401_when_no_header(monkeypatch):
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "super-secret-12345")
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get("/scim/v2/Probe")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_401_when_wrong_scheme(monkeypatch):
    """Basic auth, Digest, anything-not-Bearer → 401."""
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "super-secret-12345")
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get(
            "/scim/v2/Probe",
            headers={"Authorization": "Basic YWxpY2U6cGFzcw=="},
        )
    assert resp.status_code == 401


def test_401_when_wrong_token(monkeypatch):
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "expected-token-here")
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get(
            "/scim/v2/Probe",
            headers={"Authorization": "Bearer not-the-right-one"},
        )
    assert resp.status_code == 401
    body = resp.json()
    assert "Invalid bearer token" in body["detail"]["detail"]


def test_200_when_correct_token(monkeypatch):
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "right-token-99")
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get(
            "/scim/v2/Probe",
            headers={"Authorization": "Bearer right-token-99"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_token_is_constant_time_compared(monkeypatch):
    """A token that shares a long prefix with the real token must
    still 401. Probes the constant-time path doesn't short-circuit.
    Doesn't time-measure (flaky); just confirms behaviour matches a
    fully-different wrong token."""
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "abcdefghijklmnop-secret-12345")
    app = _make_app()
    with TestClient(app) as client:
        # Same prefix, different suffix.
        resp = client.get(
            "/scim/v2/Probe",
            headers={"Authorization": "Bearer abcdefghijklmnop-wrong-XYZ"},
        )
    assert resp.status_code == 401


def test_token_is_whitespace_stripped(monkeypatch):
    """Operators commonly land trailing newlines in K8s secret values.
    Strip whitespace from the configured token before comparison."""
    monkeypatch.setenv("SCIM_BEARER_TOKEN", "  clean-token  \n")
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get(
            "/scim/v2/Probe",
            headers={"Authorization": "Bearer clean-token"},
        )
    assert resp.status_code == 200
