"""SIEM delivery stays optional and never exposes credentials in warnings."""

import asyncio
import base64
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from server import auth
from server.routes import auth_routes
from server.services import audit_service, audit_siem


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("URL", "USER", "PASSWORD", "ORG", "STREAM"):
        monkeypatch.delenv(f"AIFACTORY_AUDIT_SIEM_{name}", raising=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUDIT_SIEM_URL", "https://siem.example/")
    monkeypatch.setenv("AIFACTORY_AUDIT_SIEM_USER", "ingester")
    monkeypatch.setenv(
        "AIFACTORY_AUDIT_SIEM_PASSWORD", "private-password"
    )  # gitleaks:allow


@pytest.fixture
def requests(monkeypatch):
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(
        audit_siem,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=3),
    )
    return captured


def test_off_by_default(requests):
    assert audit_siem.siem_config() is None
    asyncio.run(audit_siem.emit("auth.failure"))
    audit_siem.schedule("auth.failure")
    assert requests == []


@pytest.mark.parametrize("missing", ["URL", "USER", "PASSWORD"])
@pytest.mark.usefixtures("configured")
def test_empty_required_setting_disables(monkeypatch, requests, missing):
    monkeypatch.setenv(f"AIFACTORY_AUDIT_SIEM_{missing}", "")
    assert audit_siem.siem_config() is None
    asyncio.run(audit_siem.emit("auth.failure"))
    assert requests == []


@pytest.mark.usefixtures("configured")
def test_record_and_basic_auth(monkeypatch, requests):
    monkeypatch.setattr(audit_siem.time, "time", lambda: 1234.5)
    asyncio.run(
        audit_siem.emit(
            "auth.failure",
            resource_type="auth",
            reason="bad_credentials",
            ip="127.0.0.1",
        )
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://siem.example/api/default/audit_chain/_json"
    credentials = base64.b64encode(b"ingester:private-password").decode("ascii")
    assert request.headers["Authorization"] == f"Basic {credentials}"
    assert json.loads(request.content) == [
        {
            "_timestamp": 1234500000,
            "action": "auth.failure",
            "source": "aifactory",
            "resource_type": "auth",
            "reason": "bad_credentials",
            "ip": "127.0.0.1",
        }
    ]


@pytest.mark.usefixtures("configured")
def test_config_reads_custom_org_and_stream_at_call_time(monkeypatch, requests):
    assert audit_siem.siem_config()[0].endswith("/api/default/audit_chain/_json")
    monkeypatch.setenv("AIFACTORY_AUDIT_SIEM_ORG", "security")
    monkeypatch.setenv("AIFACTORY_AUDIT_SIEM_STREAM", "failures")
    monkeypatch.setenv("AIFACTORY_AUDIT_SIEM_PASSWORD", "new-password")
    asyncio.run(audit_siem.emit("auth.failure"))
    assert requests[0].url.path == "/api/security/failures/_json"
    credentials = base64.b64encode(b"ingester:new-password").decode("ascii")
    assert requests[0].headers["Authorization"] == f"Basic {credentials}"


@pytest.mark.usefixtures("configured")
def test_failure_warning_has_only_exception_type(monkeypatch, caplog):
    endpoint, auth = audit_siem.siem_config()

    def fail(request):
        raise httpx.ConnectError(
            f"{endpoint} {auth} private-password sensitive-token", request=request
        )

    monkeypatch.setattr(
        audit_siem,
        "_client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    )
    with caplog.at_level(logging.WARNING, logger=audit_siem.__name__):
        asyncio.run(audit_siem.emit("auth.failure"))
    assert [record.getMessage() for record in caplog.records] == [
        "audit SIEM push failed: ConnectError"
    ]
    for secret in (endpoint, auth, "private-password", "sensitive-token"):
        assert secret not in caplog.text


@pytest.mark.parametrize("status", [302, 401, 500])
@pytest.mark.usefixtures("configured")
def test_non_2xx_swallowed(monkeypatch, caplog, status):
    monkeypatch.setattr(
        audit_siem,
        "_client_factory",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(status))
        ),
    )
    with caplog.at_level(logging.WARNING, logger=audit_siem.__name__):
        asyncio.run(audit_siem.emit("auth.failure"))
    assert [record.getMessage() for record in caplog.records] == [
        "audit SIEM push failed: HTTPStatusError"
    ]


@pytest.mark.usefixtures("configured")
def test_schedule_without_loop():
    audit_siem.schedule("auth.failure")
    assert not audit_siem._tasks


@pytest.mark.usefixtures("configured")
def test_schedule_retains_and_discards_task(requests):
    async def run():
        audit_siem.schedule("auth.failure", reason="bad_credentials")
        assert len(audit_siem._tasks) == 1
        await asyncio.gather(*audit_siem._tasks)
        await asyncio.sleep(0)
        assert not audit_siem._tasks

    asyncio.run(run())
    assert json.loads(requests[0].content)[0]["reason"] == "bad_credentials"


def test_schedule_disabled_with_loop(monkeypatch):
    def unexpected_task(*_args, **_kwargs):
        pytest.fail("disabled SIEM must not schedule a task")

    async def run():
        with monkeypatch.context() as patch:
            patch.setattr(asyncio.get_running_loop(), "create_task", unexpected_task)
            audit_siem.schedule("auth.failure")

    asyncio.run(run())


# --- wiring (#2079): which failures reach the SIEM, and what they carry ---------


class _Conn:
    client = SimpleNamespace(host="203.0.113.7")
    url = SimpleNamespace(path="/api/x/alice@example.com")


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def _no_db(**_kw):
        return None

    monkeypatch.setattr(audit_service, "log_audit_event_bg", _no_db)
    monkeypatch.setattr(audit_siem, "schedule", lambda a, **f: calls.append((a, f)))
    return calls


async def test_auth_failure_event_has_no_path_or_token(sent):
    await auth._audit_auth_failure(_Conn(), reason="invalid_token")
    assert sent == [
        (
            "auth.failure",
            {"resource_type": "auth", "reason": "invalid_token", "ip": "203.0.113.7"},
        )
    ]


async def test_failed_login_and_bad_refresh_emit(sent):
    class _Db:
        async def rollback(self):
            self.rolled_back = True

        async def execute(self, *_a):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    login = auth_routes.LoginRequest(email="a@example.com", password="pw-123456789")  # noqa: S106 # gitleaks:allow
    with pytest.raises(HTTPException) as e:
        await auth_routes.login(login, _Conn(), _Db())
    assert e.value.status_code == 401
    refresh = auth_routes.RefreshRequest(refresh_token="not-a-jwt")  # noqa: S106 # gitleaks:allow
    with pytest.raises(HTTPException):
        await auth_routes.refresh(refresh, _Conn(), _Db())
    assert [f["reason"] for _a, f in sent] == [
        "bad_credentials",
        "invalid_refresh_token",
    ]
    assert all("a@example.com" not in str(f) for _a, f in sent)
