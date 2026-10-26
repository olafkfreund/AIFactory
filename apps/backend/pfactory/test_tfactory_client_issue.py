"""#964: build_ingest_payload must thread the origin GitHub issue into the
handoff contract provenance so TFactory can correlate the verify task with
its build + plan (the label-driven fast path carries no PFactory plan)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pfactory.tfactory_client as tc
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from trusted_plan import APPROVAL_KEY, _signing_bytes, ingest_trusted_plan, sign_plan


def _spec_dir(tmp_path: Path, req: dict[str, Any]) -> Path:
    d = tmp_path / "spec"
    d.mkdir()
    (d / "spec.md").write_text("# Feat\n\n## Acceptance Criteria\n- does the thing\n")
    (d / "requirements.json").write_text(json.dumps(req))
    return d


def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # keep the payload builder off real git/env
    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: (None, None))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(tc, "load_task_contract", lambda *_: {})


def test_issue_backfilled_from_github_issue_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {"githubIssue": {"number": 382}})
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=None)
    assert payload["contract"]["provenance"]["github_issue"] == 382


def test_issue_backfilled_from_provenance_issue_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {"provenance": {"issue_number": 382}})
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=None)
    assert payload["contract"]["provenance"]["github_issue"] == 382


def test_no_issue_leaves_contract_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {"description": "no issue here"})
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=None)
    # no contract seed + no issue → no synthetic provenance
    assert "contract" not in payload or "provenance" not in payload.get("contract", {})


def test_existing_contract_provenance_not_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: (None, None))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(
        tc, "load_task_contract", lambda *_: {"provenance": {"github_issue": 99}}
    )
    spec_dir = _spec_dir(tmp_path, {"githubIssue": {"number": 382}})
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=None)
    # setdefault: an already-signed issue wins over the requirements backfill
    assert payload["contract"]["provenance"]["github_issue"] == 99


_MISSING = r"required keyword-only argument: 'contract'"


def test_build_ingest_payload_requires_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the default comes back, the body runs: keep it off real git.
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {})
    with pytest.raises(TypeError, match=_MISSING):
        tc.build_ingest_payload(spec_dir, "001")  # type: ignore[call-arg, unused-ignore]


def test_maybe_auto_handoff_requires_contract(tmp_path: Path) -> None:
    # async def: the bad call raises before a coroutine is created
    with pytest.raises(TypeError, match=_MISSING):
        tc.maybe_auto_handoff_tfactory(tmp_path, "001")  # type: ignore[call-arg, unused-ignore]


def test_handoff_contract_carries_the_build_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1775: AIFactory sends TFactory no issue repo, so the key rides the contract
    # (TFactory prefers it) or TFactory keys the verify task on a bare "382".
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(
        tmp_path, {"githubIssue": {"number": 382}, "provenance": {"repo": "o/r"}}
    )
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=None)
    assert payload["contract"]["correlation_key"] == "o/r#382"


def test_handoff_keeps_a_pfactory_contract_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    monkeypatch.setattr(
        tc, "load_task_contract", lambda *_: {"correlation_key": "o/r#7"}
    )
    spec_dir = _spec_dir(
        tmp_path, {"githubIssue": {"number": 382}, "provenance": {"repo": "o/r"}}
    )
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=None)
    assert payload["contract"]["correlation_key"] == "o/r#7"


@pytest.mark.parametrize(
    "req",
    [
        {"metadata": {"githubIssueNumber": 382}, "provenance": {"repo": "o/r"}},
        {"githubIssue": {"number": "382", "repo": "o/r"}},
    ],
)
def test_handoff_key_follows_the_event_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, req: dict[str, Any]
) -> None:
    # #1775: tasks.py / auto-fix write metadata.githubIssueNumber, and a number can
    # be a string; the #964 provenance backfill misses both, the key must not.
    _stub_git(monkeypatch)
    payload = tc.build_ingest_payload(
        _spec_dir(tmp_path, req), "048-feat", contract=None
    )
    assert payload["contract"]["correlation_key"] == "o/r#382"


# ---- #1432: signed_contract is the pre-overlay copy ----------------------------

_SIGNED = {
    "approval": {"signature": "s"},
    "execution": {"phase_models": {"coding": "sonnet"}},
    "provenance": {"repo": "o/r"},
}


def test_signed_contract_is_pre_overlay_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    monkeypatch.setattr(tc, "_verify_phase_models", lambda *_: {"qa": "ollama"})
    spec_dir = _spec_dir(tmp_path, {"githubIssue": {"number": 382}})
    original = json.loads(json.dumps(_SIGNED))
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=_SIGNED)
    assert payload["signed_contract"] == original
    assert "github_issue" not in payload["signed_contract"]["provenance"]
    assert "correlation_key" not in payload["signed_contract"]
    assert payload["contract"]["provenance"]["github_issue"] == 382
    assert payload["contract"]["execution"]["phase_models"]["qa"] == "ollama"
    assert original == _SIGNED  # caller's dict untouched


@pytest.mark.parametrize("contract", [{}, {"provenance": {"repo": "o/r"}}])
def test_no_signed_contract_when_held_or_unsigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: dict[str, Any]
) -> None:
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {"githubIssue": {"number": 382}})
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=contract)
    assert "signed_contract" not in payload


def test_signed_contract_survives_trusted_plan_store_and_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #1432: a PFactory Ed25519 signature must still verify after the real
    # trusted-plan store (json.dumps indent=2) and load_task_contract.
    golden = json.loads(
        (
            Path(__file__).parent / "fixtures" / "contract_signature_golden.json"
        ).read_text()
    )
    plan: dict[str, Any] = {
        **golden["contract"],
        "feature": "café 🚀",
        "workflow_type": "feature",
        "ratio": 0.1 + 0.2,  # a float whose repr is not its short form
        "phases": [
            {
                "id": "p1",
                "name": "Endpoints",
                "subtasks": [
                    {
                        "id": "st1",
                        "description": "menu endpoint",
                        "status": "pending",
                        "files_to_create": ["app/menu.py"],
                    }
                ],
            }
        ],
    }
    ts, version = golden["approval_timestamp"], golden["contract_version"]
    envelope = sign_plan(
        plan, key="hmac-test-only", approved_by="pfactory",
        approval_timestamp=ts, contract_version=version,
    )  # fmt: skip
    seed = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(golden["test_only_seed"])
    )
    msg = _signing_bytes(plan, "pfactory", ts, version, kid=golden["kid"])
    envelope["pubkey_signature"] = {
        "alg": "ed25519",
        "kid": golden["kid"],
        "sig": base64.b64encode(seed.sign(msg)).decode(),
    }
    plan[APPROVAL_KEY] = envelope

    spec_dir = _spec_dir(tmp_path, {"githubIssue": {"number": 382}})
    result = ingest_trusted_plan(spec_dir, plan, keyring={"pfactory": "hmac-test-only"})
    assert result.ok, result.reasons

    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: (None, None))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(tc, "_verify_phase_models", lambda *_: {"qa": "ollama"})
    payload = tc.build_ingest_payload(spec_dir, "048-feat", contract=None)

    signed = payload["signed_contract"]
    env = signed[APPROVAL_KEY]
    sent = _signing_bytes(
        signed, env["approved_by"], env["approval_timestamp"],
        env["plan_contract_version"], kid=env["pubkey_signature"]["kid"],
    )  # fmt: skip
    assert sent == msg  # byte-for-byte
    Ed25519PublicKey.from_public_bytes(base64.b64decode(golden["public_key"])).verify(
        base64.b64decode(env["pubkey_signature"]["sig"]), sent
    )
    assert (
        payload["contract"]["provenance"]["github_issue"] == 382
    )  # overlay still there
