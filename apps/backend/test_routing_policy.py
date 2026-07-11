"""Tests for the RFC-0014 v1 per-stage routing policy (#803).

Covers the fail-closed contract (absent policy = byte-identical behaviour,
unknown tier = default), the resolution precedence (contract pinned model >
per-task override > policy tier > default), and the routing_tier stamp on the
per-worker usage records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agents.token_attribution import PromptSegments, TurnUsage, record_turn
from phase_config import DEFAULT_PHASE_MODELS, get_phase_model, resolve_model_id
from routing_policy import ENV_VAR, load_policy, policy_route, tier_for_model
from trusted_plan import execution_profile_to_metadata

_POLICY: dict[str, Any] = {
    "tiers": {"small": "haiku", "mid": "sonnet", "frontier": "opus"},
    "stages": {
        "planning": "frontier",
        "coding": "mid",
        "qa": "small",
        "qa_fixer": "small",
        "spec": "mid",
    },
}

_ALL_PHASES = ("spec", "planning", "coding", "qa", "qa_fixer")


def _spec_dir(tmp_path: Path, metadata: dict[str, Any] | None = None) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    if metadata is not None:
        (spec / "task_metadata.json").write_text(json.dumps(metadata))
    return spec


@pytest.fixture(autouse=True)
def _no_ambient_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)


# --------------------------------------------------------------------------- #
# Absent policy = byte-identical behaviour (acceptance criterion 1)
# --------------------------------------------------------------------------- #


def test_absent_policy_is_noop_for_every_phase(tmp_path: Path) -> None:
    spec = _spec_dir(tmp_path)
    for phase in _ALL_PHASES:
        expected = resolve_model_id(DEFAULT_PHASE_MODELS[phase])
        assert get_phase_model(spec, phase) == expected  # type: ignore[arg-type]


def test_absent_policy_preserves_metadata_and_cli_paths(tmp_path: Path) -> None:
    spec = _spec_dir(tmp_path, {"model": "haiku"})
    assert get_phase_model(spec, "coding") == "claude-haiku-4-5-20251001"
    assert get_phase_model(spec, "coding", cli_model="opus") == "claude-opus-4-8"
    assert load_policy() is None
    assert policy_route("coding") is None
    assert tier_for_model("claude-sonnet-4-6") is None


# --------------------------------------------------------------------------- #
# Policy resolution + precedence
# --------------------------------------------------------------------------- #


def test_policy_routes_stage_to_tier_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    spec = _spec_dir(tmp_path)
    assert get_phase_model(spec, "planning") == "claude-opus-4-8"
    assert get_phase_model(spec, "coding") == "claude-sonnet-4-6"
    assert get_phase_model(spec, "qa") == "claude-haiku-4-5-20251001"


def test_policy_loads_from_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(_POLICY))
    monkeypatch.setenv(ENV_VAR, str(policy_file))
    spec = _spec_dir(tmp_path)
    assert get_phase_model(spec, "qa") == "claude-haiku-4-5-20251001"


def test_pinned_model_outranks_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    spec = _spec_dir(
        tmp_path,
        {
            "pinnedModel": "opus",
            "isAutoProfile": True,
            "phaseModels": {"coding": "haiku"},
            "model": "haiku",
        },
    )
    assert get_phase_model(spec, "coding", cli_model="haiku") == "claude-opus-4-8"


def test_per_task_override_outranks_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    # Auto-profile per-phase choice beats the policy tier.
    spec = _spec_dir(
        tmp_path, {"isAutoProfile": True, "phaseModels": {"coding": "haiku"}}
    )
    assert get_phase_model(spec, "coding") == "claude-haiku-4-5-20251001"
    # CLI model beats the policy tier.
    plain = _spec_dir(tmp_path / "b")
    assert get_phase_model(plain, "coding", cli_model="haiku") == (
        "claude-haiku-4-5-20251001"
    )
    # Single-model metadata beats the policy tier.
    meta = _spec_dir(tmp_path / "c", {"model": "haiku"})
    assert get_phase_model(meta, "coding") == "claude-haiku-4-5-20251001"


def test_unknown_tier_fails_closed_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = {"tiers": _POLICY["tiers"], "stages": {"coding": "gigantic"}}
    monkeypatch.setenv(ENV_VAR, json.dumps(broken))
    spec = _spec_dir(tmp_path)
    assert policy_route("coding") is None
    assert get_phase_model(spec, "coding") == resolve_model_id(
        DEFAULT_PHASE_MODELS["coding"]
    )


def test_garbage_policy_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for value in ("{not json", str(tmp_path / "missing.json"), json.dumps([1, 2])):
        monkeypatch.setenv(ENV_VAR, value)
        assert load_policy() is None
        assert policy_route("coding") is None


def test_unmapped_stage_uses_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = {"tiers": _POLICY["tiers"], "stages": {"planning": "frontier"}}
    monkeypatch.setenv(ENV_VAR, json.dumps(partial))
    spec = _spec_dir(tmp_path)
    assert get_phase_model(spec, "coding") == resolve_model_id(
        DEFAULT_PHASE_MODELS["coding"]
    )


# --------------------------------------------------------------------------- #
# Contract pinned_model carriage (execution.routing.pinned_model -> metadata)
# --------------------------------------------------------------------------- #


def test_execution_profile_carries_pinned_model() -> None:
    meta = execution_profile_to_metadata(
        {"routing": {"pinned_model": "claude-opus-4-8", "class": "governed"}}
    )
    assert meta["pinnedModel"] == "claude-opus-4-8"
    # Non-string / absent pinned model is ignored.
    assert "pinnedModel" not in execution_profile_to_metadata({"routing": {}})
    assert "pinnedModel" not in execution_profile_to_metadata(
        {"routing": {"pinned_model": 3}}
    )


# --------------------------------------------------------------------------- #
# routing_tier stamp on per-worker usage records (completion envelope v1.3)
# --------------------------------------------------------------------------- #


def _record_one_turn(spec: Path) -> dict[str, Any]:
    record_turn(
        spec,
        PromptSegments(user_prompt="do the thing"),
        TurnUsage(input_tokens=100, output_tokens=50, cost_usd=0.01),
        model="claude-sonnet-4-6",
        provider="claude",
    )
    data: dict[str, Any] = json.loads((spec / "token_usage.json").read_text())
    return data["workers"]["main"]


def test_worker_record_stamps_routing_tier_with_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    rec = _record_one_turn(_spec_dir(tmp_path))
    assert rec["model"] == "claude-sonnet-4-6"
    assert rec["routing_tier"] == "mid"


def test_worker_record_has_no_tier_without_policy(tmp_path: Path) -> None:
    rec = _record_one_turn(_spec_dir(tmp_path))
    assert "routing_tier" not in rec


def test_tier_for_model_matches_shorthand_and_full_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    assert tier_for_model("sonnet") == "mid"
    assert tier_for_model("claude-sonnet-4-6") == "mid"
    assert tier_for_model("claude-opus-4-8") == "frontier"
    assert tier_for_model("ollama:qwen3:14b") is None
    assert tier_for_model(None) is None
