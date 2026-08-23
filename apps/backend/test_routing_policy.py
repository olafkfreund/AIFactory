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
from routing_policy import (
    ENV_VAR,
    contract_route,
    load_policy,
    policy_route,
    tier_for_model,
)
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
    assert get_phase_model(spec, "coding") == "claude-sonnet-5"
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


def test_explicit_choices_outrank_policy(
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


def test_policy_overrides_tier_static_metadata_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#825: the RFC-0011 tier's static ``metadata.model`` is a DEFAULT, not a
    hard pin — an active routing policy overrides it (this is what makes RFC-0014
    routing engage in the tier-labeled from-issue path). Without a policy the
    same metadata.model drives, byte-identical to before."""
    meta = _spec_dir(tmp_path, {"model": "opus"})
    # Policy ON: coding routes to the mid tier (sonnet), beating metadata.model.
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    assert get_phase_model(meta, "coding") == "claude-sonnet-5"
    # Policy OFF: metadata.model (the tier default) drives unchanged.
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert get_phase_model(meta, "coding") == resolve_model_id("opus")


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
        model="claude-sonnet-5",
        provider="claude",
        duration_ms=900,
    )
    data: dict[str, Any] = json.loads((spec / "token_usage.json").read_text())
    return data["workers"]["main"]


def test_worker_record_stamps_routing_tier_with_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    rec = _record_one_turn(_spec_dir(tmp_path))
    assert rec["model"] == "claude-sonnet-5"
    assert rec["routing_tier"] == "mid"


def test_worker_record_has_no_tier_without_policy(tmp_path: Path) -> None:
    rec = _record_one_turn(_spec_dir(tmp_path))
    assert "routing_tier" not in rec


def test_tier_for_model_matches_shorthand_and_full_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, json.dumps(_POLICY))
    assert tier_for_model("sonnet") == "mid"
    assert tier_for_model("claude-sonnet-5") == "mid"
    assert tier_for_model("claude-opus-4-8") == "frontier"
    assert tier_for_model("ollama:qwen3:14b") is None
    assert tier_for_model(None) is None


# --------------------------------------------------------------------------- #
# Contract routing block: requested / overrides / RFC-0011 floor (#803 delta)
# --------------------------------------------------------------------------- #


def test_contract_route_absent_block_is_none() -> None:
    # No metadata / empty metadata -> no-op (env policy or default handles it).
    assert contract_route("coding", None) is None
    assert contract_route("coding", {}) is None


def test_contract_requested_maps_tier_without_env_policy() -> None:
    # No AIFACTORY_ROUTING_POLICY: the contract's requested tier is mapped via
    # the built-in DEFAULT_TIERS so PFactory's policy alone can drive AIFactory.
    assert contract_route("coding", {"routingRequested": {"coding": "mid"}}) == (
        "sonnet",
        "mid",
    )
    assert contract_route("qa", {"routingRequested": {"qa": "small"}}) == (
        "haiku",
        "small",
    )


def test_contract_override_beats_requested() -> None:
    d = contract_route(
        "coding",
        {
            "routingRequested": {"coding": "frontier"},
            "routingOverrides": {"coding": "small"},
        },
    )
    assert d == ("haiku", "small")


def test_contract_uses_env_policy_tiers_map_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A local policy that maps mid -> a specific model wins the tier->model step.
    monkeypatch.setenv(
        ENV_VAR, json.dumps({"tiers": {"mid": "claude-sonnet-5"}, "stages": {}})
    )
    assert contract_route("coding", {"routingRequested": {"coding": "mid"}}) == (
        "claude-sonnet-5",
        "mid",
    )


def test_rfc0011_hard_floor_raises_coding_to_frontier() -> None:
    # Policy asked for mid, but a hard task's capability floor is frontier.
    assert contract_route(
        "coding", {"routingRequested": {"coding": "mid"}, "difficultyTier": "hard"}
    ) == ("opus", "frontier")


def test_floor_never_lowers_below_the_requested_tier() -> None:
    # A low task must NOT drag a frontier qa_full stage down.
    assert contract_route(
        "qa_full",
        {"routingRequested": {"qa_full": "frontier"}, "difficultyTier": "low"},
    ) == ("opus", "frontier")


def test_contract_unrouted_stage_is_none() -> None:
    assert contract_route("planning", {"routingRequested": {"coding": "mid"}}) is None


def test_contract_unknown_tier_is_ignored() -> None:
    assert (
        contract_route("coding", {"routingRequested": {"coding": "gigantic"}}) is None
    )


def test_get_phase_model_consumes_contract_requested(tmp_path: Path) -> None:
    # End to end through phase_config, no env policy: contract requested drives it.
    spec = _spec_dir(tmp_path, {"routingRequested": {"coding": "mid"}})
    assert get_phase_model(spec, "coding") == "claude-sonnet-5"
    # A stage the contract does not route falls back to the default.
    assert get_phase_model(spec, "planning") == resolve_model_id(
        DEFAULT_PHASE_MODELS["planning"]
    )


def test_contract_pinned_still_beats_requested(tmp_path: Path) -> None:
    spec = _spec_dir(
        tmp_path,
        {"pinnedModel": "opus", "routingRequested": {"coding": "small"}},
    )
    # pinnedModel wins over the routing block entirely.
    assert get_phase_model(spec, "coding") == resolve_model_id("opus")


def test_tier_for_model_stamps_from_contract_without_env_policy() -> None:
    # A contract routing block makes the tier stampable even with no env policy.
    meta = {"routingRequested": {"coding": "mid"}}
    assert tier_for_model("claude-sonnet-5", meta) == "mid"
    assert tier_for_model("claude-opus-4-8", meta) == "frontier"
    # Still None when neither a policy nor a contract block is present.
    assert tier_for_model("claude-sonnet-5", None) is None


def test_execution_profile_carries_routing_tiers() -> None:
    meta = execution_profile_to_metadata(
        {
            "complexity": "hard",
            "routing": {
                "requested": {"coding": "mid", "qa": "small"},
                "overrides": {"coding": "frontier"},
                "difficulty": "hard",
            },
        }
    )
    assert meta["routingRequested"] == {"coding": "mid", "qa": "small"}
    assert meta["routingOverrides"] == {"coding": "frontier"}
    assert meta["difficultyTier"] == "hard"


def test_execution_profile_difficulty_falls_back_to_complexity() -> None:
    meta = execution_profile_to_metadata(
        {"complexity": "medium", "routing": {"requested": {"coding": "small"}}}
    )
    assert meta["difficultyTier"] == "medium"


# --------------------------------------------------------------------------- #
# RFC-0011 capability floor on the env policy path (#825 follow-up)
# --------------------------------------------------------------------------- #


def test_policy_route_floors_up_to_difficulty_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Policy asks coding->small, but a HARD task's floor is frontier: raise it.
    cheap = {"tiers": _POLICY["tiers"], "stages": {"coding": "small"}}
    monkeypatch.setenv(ENV_VAR, json.dumps(cheap))
    assert policy_route("coding") == ("haiku", "small")  # no difficulty -> unchanged
    assert policy_route("coding", "hard") == ("opus", "frontier")  # floored up
    assert policy_route("coding", "medium") == ("sonnet", "mid")  # floored to mid


def test_policy_route_floor_never_lowers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Policy asks coding->frontier; a LOW task must NOT drag it down.
    rich = {"tiers": _POLICY["tiers"], "stages": {"coding": "frontier"}}
    monkeypatch.setenv(ENV_VAR, json.dumps(rich))
    assert policy_route("coding", "low") == ("opus", "frontier")


def test_get_phase_model_applies_floor_from_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hard task with a cheap coding policy is floored back to opus end to end.
    cheap = {"tiers": _POLICY["tiers"], "stages": {"coding": "small"}}
    monkeypatch.setenv(ENV_VAR, json.dumps(cheap))
    spec = _spec_dir(tmp_path, {"model": "sonnet", "difficultyTier": "hard"})
    assert get_phase_model(spec, "coding") == "claude-opus-4-8"


def test_floored_tier_falls_back_to_default_tiers_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Policy defines only the small tier but floors up to frontier -> use the
    # built-in DEFAULT_TIERS for the tier the operator did not map.
    partial = {"tiers": {"small": "haiku"}, "stages": {"coding": "small"}}
    monkeypatch.setenv(ENV_VAR, json.dumps(partial))
    assert policy_route("coding", "hard") == ("opus", "frontier")


def test_execution_profile_carries_autonomy_tier_as_difficulty() -> None:
    from trusted_plan import execution_profile_to_metadata

    meta = execution_profile_to_metadata(
        {"model": "opus", "autonomy_tier": "hard", "complexity": "complex"}
    )
    assert meta["difficultyTier"] == "hard"


class TestPhaseModelsApplyWithoutACompanionFlag:
    """#1397: `phaseModels` must select the model on its own.

    It used to be honoured only when `isAutoProfile` was ALSO set -- an
    undocumented companion flag. A request carrying nothing but
    `{"phaseModels": {"coding": "gemini-3-pro"}}` fell through to the CLI
    default and ran opus, then reported opus honestly. Truthful reporting of a
    selection that never happened is why this read as working: the only way to
    see it was to compare the model used against the model asked for, which is
    what these tests do.
    """

    def _spec(self, tmp_path, metadata):
        spec = tmp_path / "spec"
        spec.mkdir()
        (spec / "task_metadata.json").write_text(json.dumps(metadata))
        return spec

    def test_phase_models_alone_selects_the_model(self, tmp_path):
        """The exact request shape that produced #1397."""
        spec = self._spec(tmp_path, {"phaseModels": {"coding": "gemini-3-pro"}})

        assert get_phase_model(spec, "coding", cli_model="opus") == "gemini-3-pro"

    def test_is_auto_profile_still_works(self, tmp_path):
        """The flag is no longer required, but must not become poison either."""
        spec = self._spec(
            tmp_path, {"phaseModels": {"coding": "gemini-3-pro"}, "isAutoProfile": True}
        )

        assert get_phase_model(spec, "coding", cli_model="opus") == "gemini-3-pro"

    def test_an_unlisted_phase_falls_through_to_the_cli_model(self, tmp_path):
        """A partial map must not pin every other phase to the default.

        `.get(phase, DEFAULT_PHASE_MODELS[phase])` short-circuited priorities
        3-5, so naming only `planning` made `--model haiku` resolve to opus for
        coding. A phase the caller did not mention is one they left to the
        normal precedence, not one they pinned.

        haiku is used rather than opus because DEFAULT_PHASE_MODELS["coding"]
        IS opus -- asserting against opus could not tell the default from the
        CLI argument, and would pass against the defect.
        """
        spec = self._spec(
            tmp_path, {"phaseModels": {"planning": "sonnet"}, "isAutoProfile": True}
        )

        assert get_phase_model(spec, "coding", cli_model="haiku") == (
            "claude-haiku-4-5-20251001"
        )

    def test_each_phase_gets_its_own_model(self, tmp_path):
        """The point of per-phase routing: plan on one model, code on another."""
        spec = self._spec(
            tmp_path,
            {"phaseModels": {"planning": "sonnet", "coding": "haiku", "qa": "opus"}},
        )

        assert get_phase_model(spec, "planning") == "claude-sonnet-5"
        assert get_phase_model(spec, "coding") == "claude-haiku-4-5-20251001"
        assert get_phase_model(spec, "qa") == "claude-opus-4-8"

    def test_an_empty_phase_models_map_is_not_a_selection(self, tmp_path):
        """`{}` must not swallow the CLI argument."""
        spec = self._spec(tmp_path, {"phaseModels": {}})

        assert get_phase_model(spec, "coding", cli_model="haiku") == (
            "claude-haiku-4-5-20251001"
        )
