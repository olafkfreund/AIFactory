"""Tests for consuming execution.phase_routes (#2108, Factory#1959 stage 3).

Off by default: with no flag, no gateway URL, or LiteLLM set, resolution is the
same as without routes. On, the route outranks ``phaseModels`` but not a pin or
the control-plane fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from phase_config import (
    GATEWAY_TOKEN_ENV,
    GATEWAY_URL_ENV,
    PHASE_ROUTES_ENV,
    get_phase_model,
    get_phase_model_betas,
    get_provider_extra_kwargs,
    infer_provider_from_model,
    phase_route_model,
    resolve_model_id,
)
from trusted_plan import execution_profile_to_metadata

_META = {
    "isAutoProfile": True,
    "phaseModels": {"coding": "gemini-3-pro", "qa": "ollama:x"},
    "phaseRoutes": {"coding": "balanced", "qa": "cheap", "planning": "bogus"},
}


def _spec(tmp_path: Path, meta: dict[str, Any]) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(exist_ok=True)
    (spec / "task_metadata.json").write_text(json.dumps(meta))
    return spec


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        PHASE_ROUTES_ENV,
        GATEWAY_URL_ENV,
        GATEWAY_TOKEN_ENV,
        "LITELLM_GATEWAY_URL",
        "AIFACTORY_FALLBACK_MODEL",
        "AIFACTORY_ROUTING_POLICY",
        "AIFACTORY_ROUTE_RESOLUTION",
        "OPENAI_COMPATIBLE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def routes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PHASE_ROUTES_ENV, "on")
    monkeypatch.setenv(GATEWAY_URL_ENV, "http://gw:8080/")


@pytest.mark.parametrize(
    "env",
    [
        {},  # flag unset
        {PHASE_ROUTES_ENV: "off", GATEWAY_URL_ENV: "http://gw"},
        {PHASE_ROUTES_ENV: "on"},  # no gateway URL
        {
            PHASE_ROUTES_ENV: "on",
            GATEWAY_URL_ENV: "http://gw",
            "LITELLM_GATEWAY_URL": "http://l",
        },
    ],
)
def test_inert_without_all_prerequisites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = _spec(tmp_path, _META)
    assert phase_route_model("coding", _META) is None
    assert get_phase_model(spec, "coding") == resolve_model_id("gemini-3-pro")
    assert get_phase_model(spec, "qa") == resolve_model_id("ollama:x")


@pytest.mark.usefixtures("routes_on")
def test_route_wins_over_phase_models(tmp_path: Path) -> None:
    spec = _spec(tmp_path, _META)
    assert get_phase_model(spec, "coding") == "openai-compatible:balanced"
    assert get_phase_model(spec, "qa") == "openai-compatible:cheap"
    assert (
        infer_provider_from_model("openai-compatible:balanced") == "openai-compatible"
    )
    assert get_phase_model_betas(spec, "coding") == []


@pytest.mark.usefixtures("routes_on")
def test_phase_models_wins_when_no_route_resolves(tmp_path: Path) -> None:
    meta = {**_META, "phaseModels": {"planning": "opus", "spec": "sonnet"}}
    spec = _spec(tmp_path, meta)
    assert get_phase_model(spec, "planning") == resolve_model_id("opus")  # bogus class
    assert get_phase_model(spec, "spec") == resolve_model_id(
        "sonnet"
    )  # no route for spec
    no_routes = {k: v for k, v in meta.items() if k != "phaseRoutes"}
    assert (
        get_phase_model(_spec(tmp_path, no_routes), "coding")
        != "openai-compatible:balanced"
    )


@pytest.mark.usefixtures("routes_on")
def test_pin_and_fallback_outrank_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = _spec(tmp_path, {**_META, "pinnedModel": "opus"})
    assert get_phase_model(pinned, "coding") == resolve_model_id("opus")
    plain = _spec(tmp_path, _META)
    monkeypatch.setenv("AIFACTORY_FALLBACK_MODEL", "sonnet")
    assert get_phase_model(plain, "coding") == resolve_model_id("sonnet")


@pytest.mark.usefixtures("routes_on")
@pytest.mark.parametrize(
    ("route", "difficulty", "expected"),
    [
        ("local", "low", "cheap"),  # low floors at cheap, as RFC-0011 small
        ("cheap", "low", "cheap"),
        ("cheap", "medium", "balanced"),
        ("cheap", "hard", "frontier"),
        ("frontier", "low", "frontier"),  # never lowered
        ("balanced", "bogus", "balanced"),  # unknown difficulty: no floor
        ("local", None, "local"),
    ],
)
def test_difficulty_floor_raises_never_lowers(
    route: str, difficulty: str | None, expected: str
) -> None:
    meta: dict[str, Any] = {"phaseRoutes": {"coding": route}}
    if difficulty:
        meta["difficultyTier"] = difficulty
    assert phase_route_model("coding", meta) == f"openai-compatible:{expected}"


@pytest.mark.usefixtures("routes_on")
def test_class_name_goes_to_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "job-token")
    kw = get_provider_extra_kwargs("openai-compatible", "openai-compatible:frontier")
    assert kw == {
        "model": "frontier",
        "base_url": "http://gw:8080",
        "api_key": "job-token",
    }
    monkeypatch.setenv(GATEWAY_TOKEN_ENV, "gw-token")
    kw = get_provider_extra_kwargs("openai-compatible", "openai-compatible:cheap")
    assert kw["api_key"] == "gw-token"


@pytest.mark.usefixtures("routes_on")
def test_real_model_name_is_not_sent_to_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://direct")
    monkeypatch.setattr("phase_config._load_first_openai_endpoint", lambda: None)
    kw = get_provider_extra_kwargs("openai-compatible", "openai-compatible:gpt-4o-mini")
    assert kw["base_url"] == "http://direct"


def test_class_name_unchanged_when_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://direct")
    monkeypatch.setattr("phase_config._load_first_openai_endpoint", lambda: None)
    kw = get_provider_extra_kwargs("openai-compatible", "openai-compatible:cheap")
    assert kw["base_url"] == "http://direct"


def test_contract_phase_routes_reach_metadata() -> None:
    meta = execution_profile_to_metadata({"phase_routes": {"coding": "cheap"}})
    assert meta == {"phaseRoutes": {"coding": "cheap"}}  # no isAutoProfile
    meta = execution_profile_to_metadata(
        {"phase_routes": {"coding": "cheap"}, "phase_models": {"coding": "opus"}}
    )
    assert meta["phaseModels"] == {"coding": "opus"}
    assert meta["isAutoProfile"] is True
