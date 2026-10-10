"""The pod-side gate: act only on a task contract the server verified (#1673)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core import contract_trust as ct  # noqa: E402
from core.contract_trust import ENV, contract_digest, trusted_contract  # noqa: E402

C = {"deployment": {"deploy_system": "gcp-cloud-run"}, "feature": "f"}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


def _write(spec: Path, obj: Any, **dumps_kw: Any) -> None:
    (spec / "context").mkdir(parents=True, exist_ok=True)
    text = obj if isinstance(obj, str) else json.dumps(obj, **dumps_kw)
    (spec / "context" / "task_contract.json").write_text(text)


def test_verified_returns_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, C)
    monkeypatch.setenv(ENV, contract_digest(C))
    state, contract, _why = trusted_contract(tmp_path)
    assert (state, contract) == ("verified", C)


def test_verified_survives_reformatting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, dict(reversed(list(C.items()))), indent=4)
    other_order = {"feature": "f", "deployment": {"deploy_system": "gcp-cloud-run"}}
    monkeypatch.setenv(ENV, contract_digest(other_order))
    state, contract, _why = trusted_contract(tmp_path)
    assert (state, contract) == ("verified", C)


def test_env_hold_holds_by_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, C)
    monkeypatch.setenv(ENV, "hold")
    assert trusted_contract(tmp_path) == ("hold", C, "held by server")


def test_digest_mismatch_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, C)
    changed = {**C, "deployment": {"deploy_system": "aws-app-runner"}}
    monkeypatch.setenv(ENV, contract_digest(changed))
    state, _contract, why = trusted_contract(tmp_path)
    assert (state, why) == ("hold", "digest mismatch")


@pytest.mark.parametrize("bad", ["", "zz", "UPPER"])
def test_bad_env_value_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _write(tmp_path, C)
    value = contract_digest(C).upper() if bad == "UPPER" else bad
    monkeypatch.setenv(ENV, value)
    state, _contract, why = trusted_contract(tmp_path)
    assert (state, why) == ("hold", "digest mismatch")


def test_missing_file_under_digest_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, contract_digest(C))
    assert trusted_contract(tmp_path) == ("hold", None, "digest mismatch")


@pytest.mark.parametrize("content", [None, "[1]", "{"])
@pytest.mark.parametrize("env", [None, "hold"])
def test_missing_or_bad_file_is_silent_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    content: str | None,
    env: str | None,
) -> None:
    if content is not None:
        _write(tmp_path, content)
    if env is not None:
        monkeypatch.setenv(ENV, env)
    with caplog.at_level(logging.DEBUG):
        state, contract, _why = trusted_contract(tmp_path)
    assert (state, contract) == ("legacy", None)
    assert "[trusted-contract]" not in caplog.text


def test_trace_without_env_holds(tmp_path: Path) -> None:
    _write(tmp_path, {**C, "approval": {"sig": "x"}})
    (tmp_path / "requirements.json").write_text(
        json.dumps({"provenance": {"trusted_plan": True}})
    )
    state, _contract, why = trusted_contract(tmp_path)
    assert (state, why) == ("hold", "trusted trace without a server verdict")


def test_no_env_no_trace_is_legacy(tmp_path: Path) -> None:
    _write(tmp_path, C)
    state, contract, _why = trusted_contract(tmp_path)
    assert (state, contract) == ("legacy", C)


def test_exception_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, C)

    def boom(_spec: Path) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(ct, "has_trusted_trace", boom)
    state, _contract, _why = trusted_contract(tmp_path)
    assert state == "hold"


def test_env_read_at_call_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, C)
    assert trusted_contract(tmp_path)[0] == "legacy"
    monkeypatch.setenv(ENV, "hold")
    assert trusted_contract(tmp_path)[0] == "hold"


def test_has_trusted_trace_sources(tmp_path: Path) -> None:
    a, b, c, d = (tmp_path / n for n in "abcd")
    _write(a, {**C, "approval": {}})
    b.mkdir()
    (b / "implementation_plan.json").write_text(json.dumps({"approval": {}}))
    c.mkdir()
    (c / "requirements.json").write_text(
        json.dumps({"provenance": {"trusted_plan": True}})
    )
    d.mkdir()
    assert [ct.has_trusted_trace(p) for p in (a, b, c, d)] == [True, True, True, False]
