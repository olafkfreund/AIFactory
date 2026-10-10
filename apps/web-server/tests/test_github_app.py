# ruff: noqa: S105, S106
"""AIFactory mints a GitHub App installation token at boot (#1671)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

WEB_SERVER = Path(__file__).resolve().parents[1]
# ``core`` lives in apps/backend; github_app imports it only lazily, so put it
# on sys.path here rather than depend on another import's side effect.
BACKEND = WEB_SERVER.parent / "backend"
for _root in (WEB_SERVER, BACKEND):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from core import mcp_credentials as mc  # noqa: E402
from server.services import github_app  # noqa: E402

_APP_VARS = (
    "AIFACTORY_GITHUB_APP_ID",
    "AIFACTORY_GITHUB_APP_INSTALLATION_ID",
    "AIFACTORY_GITHUB_APP_PRIVATE_KEY",
)
_PAT_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")
_KEY = "AIFACTORY_GITHUB_APP_PRIVATE_KEY"


class _Post:
    """Stands in for ``httpx.post``; yields queued responses or raises them."""

    def __init__(self) -> None:
        self.queue: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"url": url, **kw})
        if not self.queue:
            raise AssertionError("unexpected POST")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Post:
    for name in (*_APP_VARS, *_PAT_VARS):
        # setenv first so monkeypatch records the original and undoes what
        # start() later writes straight into os.environ.
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setattr(mc, "OPERATOR_CONFIG_PATH", tmp_path / "mcp-credentials.json")
    mc.reset_cache()
    for attr in ("_key", "_app_id", "_installation_id"):
        monkeypatch.setattr(github_app, attr, None)
    post = _Post()
    monkeypatch.setattr(github_app.httpx, "post", post)
    yield post
    mc.reset_cache()


@pytest.fixture(scope="module")
def rsa_pem() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


def _app_env(mp: pytest.MonkeyPatch, pem: str) -> None:
    mp.setenv("AIFACTORY_GITHUB_APP_ID", "123")
    mp.setenv("AIFACTORY_GITHUB_APP_INSTALLATION_ID", "456")
    mp.setenv(_KEY, pem)


def _resp(token: str, status: int = 201) -> httpx.Response:
    return httpx.Response(
        status,
        json={"token": token, "expires_at": "2099-01-01T00:00:00Z"},
        request=httpx.Request("POST", "https://api.github.com/x"),
    )


def test_no_app_vars_is_a_noop(_isolate: _Post) -> None:
    github_app.start()
    assert not github_app.configured()
    assert "GH_TOKEN" not in os.environ
    assert not _isolate.calls


@pytest.mark.parametrize(
    "subset",
    [s for n in (1, 2) for s in itertools.combinations(_APP_VARS, n)],
)
def test_partial_app_vars_refuse_to_start(
    subset: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, _isolate: _Post
) -> None:
    for name in subset:
        monkeypatch.setenv(name, "v")
    with pytest.raises(RuntimeError):
        github_app.start()
    assert not _isolate.calls
    assert not github_app.configured()


@pytest.mark.parametrize("var", _PAT_VARS)
def test_boot_pat_refuses_to_start(
    var: str,
    rsa_pem: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    _isolate: _Post,
) -> None:
    _app_env(monkeypatch, rsa_pem[0])
    monkeypatch.setenv(var, "ghp_boot")
    with pytest.raises(RuntimeError) as exc:
        github_app.start()
    assert var in str(exc.value)
    assert "ghp_boot" not in str(exc.value)
    assert not _isolate.calls


def test_token_env_named_pat_refuses_to_start(
    rsa_pem: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    _isolate: _Post,
) -> None:
    _app_env(monkeypatch, rsa_pem[0])
    cfg = mc.OPERATOR_CONFIG_PATH
    cfg.write_text(json.dumps({"github": {"tokenEnv": "OPS_GH_PAT"}}))
    cfg.chmod(0o600)
    monkeypatch.setenv("OPS_GH_PAT", "ghp_ops")
    with pytest.raises(RuntimeError) as exc:
        github_app.start()
    assert "OPS_GH_PAT" in str(exc.value)
    assert "ghp_ops" not in str(exc.value)
    assert not _isolate.calls


def test_empty_pat_var_does_not_refuse(
    rsa_pem: tuple[str, str], monkeypatch: pytest.MonkeyPatch, _isolate: _Post
) -> None:
    _app_env(monkeypatch, rsa_pem[0])
    monkeypatch.setenv("GITHUB_TOKEN", "")
    _isolate.queue.append(_resp("ghs_1"))
    github_app.start()
    assert os.environ["GH_TOKEN"] == "ghs_1"


def _hosts(tmp_path: Path, text: str) -> None:
    (tmp_path / "gh").mkdir()
    (tmp_path / "gh" / "hosts.yml").write_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "github.com:\n  oauth_token: gho_x\n  user: u\n",
        "github.com:\n  user: u\n  users:\n    u:\n      oauth_token: gho_x\n",
    ],
    ids=["legacy", "nested"],
)
def test_hosts_yml_github_token_refuses_to_start(
    text: str,
    rsa_pem: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate: _Post,
) -> None:
    _app_env(monkeypatch, rsa_pem[0])
    _hosts(tmp_path, text)
    with pytest.raises(RuntimeError) as exc:
        github_app.start()
    assert "gho_x" not in str(exc.value)
    assert not _isolate.calls


def test_hosts_yml_other_host_starts(
    rsa_pem: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _isolate: _Post,
) -> None:
    _app_env(monkeypatch, rsa_pem[0])
    _hosts(tmp_path, "ghe.example.com:\n  oauth_token: x\n")
    _isolate.queue.append(_resp("ghs_1"))
    github_app.start()
    assert github_app.configured()


def test_start_mints_and_moves_the_key(
    rsa_pem: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _isolate: _Post,
) -> None:
    private, public = rsa_pem
    _app_env(monkeypatch, private)
    _isolate.queue.append(_resp("ghs_1"))
    with caplog.at_level(logging.DEBUG):
        github_app.start()
    assert _KEY not in os.environ
    assert os.environ["GH_TOKEN"] == "ghs_1"
    assert os.environ["GITHUB_TOKEN"] == "ghs_1"
    assert github_app.configured()
    (call,) = _isolate.calls
    assert call["url"].endswith("/app/installations/456/access_tokens")
    auth = call["headers"]["Authorization"]
    assert auth.startswith("Bearer ")
    tok = auth.removeprefix("Bearer ")
    assert jwt.get_unverified_header(tok)["alg"] == "RS256"
    claims = jwt.decode(tok, public, algorithms=["RS256"])
    assert str(claims["iss"]) == "123"
    assert claims["exp"] - claims["iat"] <= 600
    assert claims["iat"] <= time.time()
    assert "ghs_1" not in caplog.text


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("boom"),
        _resp("ghs_500", 500),  # non-empty, so only raise_for_status catches it
        _resp(""),
    ],
    ids=["connect", "http500", "empty_token"],
)
def test_mint_error_at_start_raises(
    outcome: Any,
    rsa_pem: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    _isolate: _Post,
) -> None:
    _app_env(monkeypatch, rsa_pem[0])
    _isolate.queue.append(outcome)
    with pytest.raises(Exception):  # noqa: B017, PT011
        github_app.start()
    assert "GH_TOKEN" not in os.environ
    assert "GITHUB_TOKEN" not in os.environ


def test_refresh_replaces_on_success_keeps_on_failure(
    rsa_pem: tuple[str, str], monkeypatch: pytest.MonkeyPatch, _isolate: _Post
) -> None:
    _app_env(monkeypatch, rsa_pem[0])
    _isolate.queue.append(_resp("ghs_1"))
    github_app.start()
    _isolate.queue.extend([httpx.ConnectError("boom"), _resp(""), _resp("ghs_2")])
    stop = asyncio.Event()
    log: list[tuple[float, str]] = []

    async def fake_wait_for(coro: Any, timeout: float) -> None:
        coro.close()
        log.append((timeout, os.environ["GH_TOKEN"]))
        if len(log) == 4:
            stop.set()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    asyncio.run(github_app.refresh_loop(stop))
    assert log == [(1800, "ghs_1"), (60, "ghs_1"), (60, "ghs_1"), (1800, "ghs_2")]
    assert os.environ["GH_TOKEN"] == "ghs_2"
    assert os.environ["GITHUB_TOKEN"] == "ghs_2"


def test_refresh_loop_exits_when_stopped() -> None:
    async def run() -> None:
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(github_app.refresh_loop(stop), 1)

    asyncio.run(run())
