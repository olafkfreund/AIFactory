"""Tests for the LiteLLM admin-API client (Epic #35 #38 PR-2b).

Covers (design §3 + implementation plan PR-2):
- ``create_virtual_key`` posts /key/generate with the documented body
  shape + returns the generated key.
- ``update_virtual_key`` posts /key/update with the documented body.
- ``disable_virtual_key`` sets budget_duration='0s'.
- ``delete_virtual_key`` posts /key/delete.
- ``list_virtual_keys`` returns a list of dicts.
- Construction with a plaintext LITELLM_MASTER_KEY env raises
  ValueError (design §3 anti-pattern guard).
- Construction without LITELLM_GATEWAY_URL or explicit base_url
  raises ValueError.
- Each admin call invokes the master-key provider exactly once
  (per-request KMS unwrap, no in-process cache).
- Network failure raises LiteLLMAdminUnavailableError; HTTP 4xx/5xx
  raises LiteLLMAdminError.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_SERVER = REPO_ROOT / 'apps' / 'web-server'
if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Construction-time validation
# ---------------------------------------------------------------------------


def test_construction_rejects_plaintext_master_key_env(monkeypatch):
    """Design §3: plaintext LITELLM_MASTER_KEY in env is the explicit
    anti-pattern. The client must refuse to start."""
    monkeypatch.setenv('LITELLM_MASTER_KEY', 'sk-plaintext-leak')
    monkeypatch.setenv('LITELLM_GATEWAY_URL', 'http://litellm:4000')

    from server.services.litellm_admin_client import LiteLLMAdminClient

    with pytest.raises(ValueError, match='plaintext'):
        LiteLLMAdminClient(master_key_provider=lambda: 'sk-test')


def test_construction_requires_base_url(monkeypatch):
    monkeypatch.delenv('LITELLM_GATEWAY_URL', raising=False)
    monkeypatch.delenv('LITELLM_MASTER_KEY', raising=False)
    from server.services.litellm_admin_client import LiteLLMAdminClient

    with pytest.raises(ValueError, match='base_url'):
        LiteLLMAdminClient()


# ---------------------------------------------------------------------------
# Round-trip happy path via mocked HTTP transport
# ---------------------------------------------------------------------------


class _MockTransport(httpx.AsyncBaseTransport):
    """Captures all requests + returns scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(500, json={'error': 'no script left'})
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _clean_master_key_env(monkeypatch):
    """Every test runs without the anti-pattern env set."""
    monkeypatch.delenv('LITELLM_MASTER_KEY', raising=False)
    monkeypatch.delenv('LITELLM_MASTER_KEY_WRAPPED', raising=False)


def _client_with_transport(transport):
    """Construct an admin client whose internal httpx.AsyncClient
    uses our mock transport. Monkeypatching the constructor would be
    simpler but the production path constructs the client per call;
    instead we patch the module's httpx import."""
    from server.services import litellm_admin_client as mod

    captured: dict = {}
    orig_async_client = httpx.AsyncClient

    def _patched_async_client(*args, **kwargs):
        kwargs['transport'] = transport
        client = orig_async_client(*args, **kwargs)
        captured['client'] = client
        return client

    return mod, _patched_async_client


def test_create_virtual_key_posts_expected_shape(monkeypatch):
    transport = _MockTransport([
        httpx.Response(200, json={'key': 'sk-test-12345'}),
    ])
    mod, patched = _client_with_transport(transport)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', patched)

    provider_calls = {'n': 0}

    def _provider():
        provider_calls['n'] += 1
        return 'sk-master-test'

    client = mod.LiteLLMAdminClient(
        base_url='http://litellm:4000',
        master_key_provider=_provider,
    )

    key = _run(client.create_virtual_key(
        org_id='org-acme',
        allowed_models=['claude-*'],
        budget_usd_per_day=100.0,
    ))
    assert key == 'sk-test-12345'

    # Master key unwrapped exactly once.
    assert provider_calls['n'] == 1

    # Posted shape.
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.method == 'POST'
    assert req.url.path == '/key/generate'
    body = req.content.decode()
    assert 'org-acme' in body
    assert 'claude-*' in body
    assert '100' in body
    # Authorization header carries the master key (per-request, not env).
    assert req.headers['authorization'] == 'Bearer sk-master-test'


def test_update_virtual_key_sends_models_and_budget(monkeypatch):
    transport = _MockTransport([httpx.Response(200, json={})])
    mod, patched = _client_with_transport(transport)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', patched)

    client = mod.LiteLLMAdminClient(
        base_url='http://litellm:4000',
        master_key_provider=lambda: 'sk-master',
    )
    _run(client.update_virtual_key(
        org_id='org-acme',
        allowed_models=['gpt-4o*'],
        budget_duration='7d',
        max_budget=500.0,
    ))

    assert len(transport.requests) == 1
    body = transport.requests[0].content.decode()
    assert 'gpt-4o*' in body
    assert '7d' in body
    assert '500' in body


def test_disable_virtual_key_zeroes_budget(monkeypatch):
    transport = _MockTransport([httpx.Response(200, json={})])
    mod, patched = _client_with_transport(transport)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', patched)

    client = mod.LiteLLMAdminClient(
        base_url='http://litellm:4000',
        master_key_provider=lambda: 'sk-master',
    )
    _run(client.disable_virtual_key(org_id='org-acme'))

    body = transport.requests[0].content.decode()
    assert '0s' in body
    # max_budget=0.0 is the immediate-block idiom.
    assert '"max_budget": 0' in body or '"max_budget":0' in body


def test_delete_virtual_key_posts_user_ids(monkeypatch):
    transport = _MockTransport([httpx.Response(200, json={})])
    mod, patched = _client_with_transport(transport)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', patched)

    client = mod.LiteLLMAdminClient(
        base_url='http://litellm:4000',
        master_key_provider=lambda: 'sk-master',
    )
    _run(client.delete_virtual_key(org_id='org-acme'))

    req = transport.requests[0]
    assert req.url.path == '/key/delete'
    body = req.content.decode()
    assert 'org-acme' in body
    assert 'user_ids' in body


def test_list_virtual_keys_returns_list(monkeypatch):
    transport = _MockTransport([
        httpx.Response(200, json={'keys': [
            {'metadata': {'aifactory_org_id': 'org-a'}},
            {'metadata': {'aifactory_org_id': 'org-b'}},
        ]}),
    ])
    mod, patched = _client_with_transport(transport)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', patched)

    client = mod.LiteLLMAdminClient(
        base_url='http://litellm:4000',
        master_key_provider=lambda: 'sk-master',
    )
    keys = _run(client.list_virtual_keys())
    assert len(keys) == 2
    assert keys[0]['metadata']['aifactory_org_id'] == 'org-a'


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_http_4xx_raises_LiteLLMAdminError(monkeypatch):
    transport = _MockTransport([
        httpx.Response(400, text='bad request: missing user_id'),
    ])
    mod, patched = _client_with_transport(transport)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', patched)

    client = mod.LiteLLMAdminClient(
        base_url='http://litellm:4000',
        master_key_provider=lambda: 'sk-master',
    )

    with pytest.raises(mod.LiteLLMAdminError) as excinfo:
        _run(client.update_virtual_key(org_id='org-x'))
    assert excinfo.value.status == 400
    assert 'bad request' in excinfo.value.body


def test_network_error_raises_LiteLLMAdminUnavailableError(monkeypatch):
    class _BoomTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError('connection refused')

    mod, patched = _client_with_transport(_BoomTransport())
    monkeypatch.setattr(mod.httpx, 'AsyncClient', patched)

    client = mod.LiteLLMAdminClient(
        base_url='http://litellm:4000',
        master_key_provider=lambda: 'sk-master',
    )

    with pytest.raises(mod.LiteLLMAdminUnavailableError):
        _run(client.disable_virtual_key(org_id='org-acme'))
