"""Tests for ``intake.execution_block.build_execution_block`` (RFC-0011 #635).

Pure tier -> execution-block mapping; the low-tier Ollama probe is injected so
no network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from intake.execution_block import build_execution_block
from pfactory.tiers import Tier


def test_low_tier_uses_injected_resolver():
    block = build_execution_block(Tier.LOW, low_model_resolver=lambda: "ollama:qwen3")
    assert block["model"] == "ollama:qwen3"
    assert block["skip_planning"] is True
    assert block["review_tier"] == "auto"
    assert block["complexity"] == "simple"
    assert block["autonomy_tier"] == "low"


def test_medium_tier():
    block = build_execution_block(Tier.MEDIUM)
    assert block["model"] == "sonnet"
    assert block["skip_planning"] is True
    assert block["review_tier"] == "async"
    assert block["complexity"] == "standard"
    assert block["autonomy_tier"] == "medium"


def test_hard_tier_full_planning():
    block = build_execution_block(Tier.HARD)
    assert block["model"] == "opus"
    assert block["skip_planning"] is False  # hard runs full decompose
    assert block["review_tier"] == "blocking"
    assert block["complexity"] == "complex"
    assert block["autonomy_tier"] == "hard"


def test_change_mode_carried_through():
    block = build_execution_block(Tier.HARD, change_mode="migration")
    assert block["change_mode"] == "migration"


def test_change_mode_absent_when_not_given():
    assert "change_mode" not in build_execution_block(Tier.MEDIUM)


def test_low_tier_default_resolver_probes_ollama(monkeypatch):
    # Without an injected resolver it falls back to resolve_low_tier_model;
    # patch that to avoid the network.
    import intake.execution_block as eb

    monkeypatch.setattr(eb, "resolve_low_tier_model", lambda: "haiku")
    assert build_execution_block(Tier.LOW)["model"] == "haiku"


def test_rejects_non_tier():
    with pytest.raises(TypeError):
        build_execution_block("low")  # type: ignore[arg-type]


# ── parallel / workers (label > setting > env > off) ───────────────────────


@pytest.fixture(autouse=True)
def _clean_parallel_env(monkeypatch, tmp_path):
    """Never inherit an ambient default into these assertions.

    Isolates both rungs of the fallback chain: the env vars, and the portal
    setting mirrored into ~/.aifactory/config.json (#905) which
    build_execution_block now reads via Path.home().
    """
    monkeypatch.delenv("AIFACTORY_INTAKE_PARALLEL", raising=False)
    monkeypatch.delenv("AIFACTORY_INTAKE_WORKERS", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def test_parallel_defaults_off_when_unlabelled_and_unset() -> None:
    block = build_execution_block(Tier.MEDIUM)
    assert block["parallel"] is False
    assert "workers" not in block


def test_parallel_label_enables_regardless_of_tier() -> None:
    for tier in (Tier.LOW, Tier.MEDIUM, Tier.HARD):
        block = build_execution_block(
            tier, parallel=True, low_model_resolver=lambda: "ollama:qwen3"
        )
        assert block["parallel"] is True


def test_workers_emitted_only_when_parallel_is_on() -> None:
    on = build_execution_block(Tier.MEDIUM, parallel=True, workers=4)
    assert on["parallel"] is True and on["workers"] == 4

    off = build_execution_block(Tier.MEDIUM, parallel=False, workers=4)
    assert off["parallel"] is False
    assert "workers" not in off  # a cap is meaningless to a serial build


def test_env_default_enables_when_unlabelled(monkeypatch) -> None:
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "true")
    monkeypatch.setenv("AIFACTORY_INTAKE_WORKERS", "5")
    block = build_execution_block(Tier.MEDIUM)
    assert block["parallel"] is True
    assert block["workers"] == 5


def test_explicit_label_beats_env_default_both_ways(monkeypatch) -> None:
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "1")
    assert build_execution_block(Tier.MEDIUM, parallel=False)["parallel"] is False

    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "off")
    assert build_execution_block(Tier.MEDIUM, parallel=True)["parallel"] is True


def test_workers_label_beats_env_default(monkeypatch) -> None:
    monkeypatch.setenv("AIFACTORY_INTAKE_WORKERS", "5")
    block = build_execution_block(Tier.MEDIUM, parallel=True, workers=2)
    assert block["workers"] == 2


def test_falsy_and_malformed_env_values_leave_parallel_off(monkeypatch) -> None:
    for val in ("0", "false", "no", "off", "", "  ", "banana"):
        monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", val)
        assert build_execution_block(Tier.MEDIUM)["parallel"] is False


def test_malformed_env_workers_falls_back_to_coder_default(monkeypatch) -> None:
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "on")
    for val in ("0", "-3", "abc", "2.5"):
        monkeypatch.setenv("AIFACTORY_INTAKE_WORKERS", val)
        block = build_execution_block(Tier.MEDIUM)
        assert block["parallel"] is True
        assert "workers" not in block  # DEFAULT_PARALLEL_WORKERS decides


# ── #905: the portal setting rung ──────────────────────────────────────────
#
# The portal toggle used to drive nothing — intake read only the env var. These
# pin the full precedence chain: label > setting > env > off.


def _write_setting(home: Path, **parallel_block: object) -> None:
    """Mirror a portal preference the way settings.py _mirror_to_global_config does."""
    config_dir = home / ".aifactory"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"parallel": parallel_block}))


def test_setting_enables_when_unlabelled_and_env_unset(tmp_path) -> None:
    """The #905 bug: this was False because nothing read the portal setting."""
    _write_setting(tmp_path, enabled=True, workers=6)

    block = build_execution_block(Tier.MEDIUM)

    assert block["parallel"] is True
    assert block["workers"] == 6


def test_setting_beats_env_both_ways(tmp_path, monkeypatch) -> None:
    """The user's own default outranks the fleet/operator env default."""
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "1")
    _write_setting(tmp_path, enabled=False, workers=3)
    assert build_execution_block(Tier.MEDIUM)["parallel"] is False

    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "off")
    _write_setting(tmp_path, enabled=True, workers=3)
    assert build_execution_block(Tier.MEDIUM)["parallel"] is True


def test_serial_label_beats_a_setting_that_says_parallel(tmp_path) -> None:
    """Per-issue intent is the most specific rung and wins outright."""
    _write_setting(tmp_path, enabled=True, workers=6)

    block = build_execution_block(Tier.MEDIUM, parallel=False)

    assert block["parallel"] is False
    assert "workers" not in block


def test_parallel_label_beats_a_setting_that_says_serial(tmp_path) -> None:
    _write_setting(tmp_path, enabled=False, workers=6)

    block = build_execution_block(Tier.MEDIUM, parallel=True)

    assert block["parallel"] is True
    assert block["workers"] == 6  # cap still falls through to the setting


def test_workers_precedence_label_then_setting_then_env(tmp_path, monkeypatch) -> None:
    """Each rung of the cap chain, narrowed one at a time."""
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "on")
    monkeypatch.setenv("AIFACTORY_INTAKE_WORKERS", "5")

    # env only
    assert build_execution_block(Tier.MEDIUM)["workers"] == 5

    # setting outranks env
    _write_setting(tmp_path, enabled=True, workers=6)
    assert build_execution_block(Tier.MEDIUM)["workers"] == 6

    # label outranks setting
    assert build_execution_block(Tier.MEDIUM, workers=2)["workers"] == 2


def test_setting_absent_or_corrupt_falls_through_to_env(tmp_path, monkeypatch) -> None:
    """A broken config must not break intake, nor swallow the env default."""
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "on")
    config_dir = tmp_path / ".aifactory"
    config_dir.mkdir(parents=True, exist_ok=True)

    for content in ("", "not json", "[]", "null", '{"parallel": "yes"}', "{}"):
        (config_dir / "config.json").write_text(content)
        assert build_execution_block(Tier.MEDIUM)["parallel"] is True


def test_setting_workers_ignores_unusable_values(tmp_path) -> None:
    """Garbage caps fall through rather than pinning a nonsense worker count."""
    for bad in (True, 0, -3, "4", 2.5, None):
        _write_setting(tmp_path, enabled=True, workers=bad)
        block = build_execution_block(Tier.MEDIUM)
        assert block["parallel"] is True
        assert "workers" not in block  # DEFAULT_PARALLEL_WORKERS decides
