"""Reviewer selection never uses a coding or fixer model."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from qa import reviewer  # noqa: E402
from qa.independence import (  # noqa: E402
    IndependentReviewUnavailable,
    is_configured,
    pick_reviewer,
)
from qa.loop import _write_evidence  # noqa: E402


def gates(model="sonnet", routes=None):
    return SimpleNamespace(
        model=lambda _: model, metadata={"phaseRoutes": routes or {}}
    )


def identity(model, provider="claude", served_model=None, phase="coding"):
    return {
        "model": model,
        "provider": provider,
        "served_model": served_model,
        "phase": phase,
    }


def test_equal_model_and_alias_are_replaced():
    for model in ("sonnet", "claude-sonnet-5-5"):
        pick = pick_reviewer(
            gates(model),
            [identity("sonnet")],
            configured=lambda m: m in {"sonnet", "opus"},
        )
        assert pick.model == "opus"
        assert pick.override


def test_pin_is_overridden_for_qa_only():
    pinned = SimpleNamespace(
        model=lambda _: "sonnet", metadata={"pinnedModel": "sonnet"}
    )
    pick = pick_reviewer(pinned, [identity("sonnet")], configured=lambda m: m == "opus")
    assert pick.model == "opus"
    assert pinned.metadata["pinnedModel"] == "sonnet"


def test_provider_preference_beats_distinct_qa_model(monkeypatch):
    monkeypatch.setenv("AIFACTORY_RUNTIMES", "codex")
    monkeypatch.setenv("AIFACTORY_QA_ALT_MODELS", "gpt-5.3-codex")
    pick = pick_reviewer(
        gates("opus"),
        [identity("sonnet")],
        configured=lambda m: m in {"opus", "gpt-5.3-codex"},
    )
    assert pick.model == "gpt-5.3-codex"
    assert pick.provider == "codex"
    assert pick.override


def test_denied_first_candidate_moves_on(monkeypatch):
    from model_registry import ModelNotApprovedError

    monkeypatch.setenv("AIFACTORY_MODEL_REGISTRY_ENFORCE", "deny")
    monkeypatch.setenv(
        "AIFACTORY_MODEL_REGISTRY", '{"claude-opus-5-5": {"stages": ["qa"]}}'
    )
    denied_gates = SimpleNamespace(
        model=lambda _: (_ for _ in ()).throw(ModelNotApprovedError("denied")),
        cli_model=None,
        metadata={"phaseModels": {"qa": "sonnet"}},
    )
    pick = pick_reviewer(denied_gates, [], configured=lambda m: m in {"sonnet", "opus"})
    assert pick.model == "opus"


def test_equal_route_class_or_unknown_served_model_reselects():
    coding = [identity("openai-compatible:balanced", "openai-compatible")]
    for route in ("balanced", "frontier"):
        pick = pick_reviewer(
            gates("openai-compatible:frontier", {"qa": route, "coding": "balanced"}),
            coding,
            configured=lambda m: m in {"openai-compatible:frontier", "opus"},
        )
        assert pick.model == "opus"


def test_no_distinct_configured_reviewer():
    with pytest.raises(IndependentReviewUnavailable):
        pick_reviewer(
            gates("sonnet"), [identity("sonnet")], configured=lambda m: m == "sonnet"
        )


def test_failover_filter_keeps_provider_distinct(monkeypatch):
    monkeypatch.setenv("AIFACTORY_RUNTIMES", "codex")
    monkeypatch.setenv("AIFACTORY_QA_ALT_MODELS", "gpt-5.3-codex")
    pick = pick_reviewer(
        gates("sonnet"), [identity("sonnet")], configured=lambda m: m == "gpt-5.3-codex"
    )
    assert pick.provider == "codex"


def test_disabled_runtime_is_never_picked(monkeypatch):
    monkeypatch.setenv("AIFACTORY_RUNTIMES", "")
    monkeypatch.setenv("AIFACTORY_QA_ALT_MODELS", "gpt-5.3-codex")
    monkeypatch.setattr("qa.independence.shutil.which", lambda _name: "/bin/codex")
    monkeypatch.setenv("CODEX_API_KEY", "present")
    assert is_configured("gpt-5.3-codex")
    pick = pick_reviewer(
        gates("sonnet"),
        [identity("sonnet")],
        configured=lambda m: m == "opus" or is_configured(m),
    )
    assert pick.model == "opus"


def test_saved_openai_endpoints_are_configured(monkeypatch):
    import phase_config

    endpoint = {
        "base_url": "http://localhost:1234",
        "api_key": None,
        "default_model": "local",
    }
    monkeypatch.setattr(phase_config, "_load_first_openai_endpoint", lambda: endpoint)
    monkeypatch.setattr(
        phase_config,
        "_load_openai_endpoint_by_label",
        lambda label: endpoint if label == "local" else None,
    )
    assert is_configured("openai:local:custom")
    assert is_configured("openai-compatible:custom")


def test_studio_model_uses_google_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "present")
    assert is_configured("studio:gemini-2.5-flash")
    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert not is_configured("studio:gemini-2.5-flash")


@pytest.mark.asyncio
async def test_verdict_writing_fallback_is_filtered_and_recorded(tmp_path, monkeypatch):
    from qa import independence

    class Provider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def query(self, _prompt):
            pass

        async def receive_response(self):
            if False:
                yield None

    built = []
    monkeypatch.setattr(independence, "is_configured", lambda model: model == "opus")
    monkeypatch.setattr(
        reviewer,
        "get_provider",
        lambda provider, **kwargs: built.append((provider, kwargs["model"]))
        or Provider(),
    )
    monkeypatch.setattr(
        reviewer, "get_qa_signoff_status", lambda _spec: {"status": "approved"}
    )
    writer = {"model": "sonnet", "provider": "claude", "override": None}
    result = await reviewer._run_tool_fallback(
        "analysis",
        tmp_path,
        tmp_path,
        1,
        None,
        identities=[identity("sonnet")],
        verdict_writer=writer,
    )
    assert result == ("approved", "analysis")
    assert built == [("claude", "opus")]
    assert writer["model"] == "opus"
    assert writer["override"]


def test_pick_override_is_written_through_evidence_writer(tmp_path, monkeypatch):
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "qa_report.md").write_text("approved")
    monkeypatch.setattr("qa.loop.subprocess.check_output", lambda *_a, **_kw: "head\n")
    pick = pick_reviewer(
        gates("sonnet"), [identity("sonnet")], configured=lambda m: m == "opus"
    )
    assert _write_evidence(
        tmp_path,
        worktree,
        source,
        status="approved",
        model=pick.model,
        provider=pick.provider,
        cycle=1,
        override=pick.override,
    )
    assert (
        json.loads((source / "qa_evidence.json").read_text())["override"]
        == pick.override
    )


@pytest.mark.asyncio
async def test_unavailable_is_recorded_without_running_qa(tmp_path, monkeypatch):
    from qa import loop

    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(loop, "is_build_complete", lambda _: True)
    monkeypatch.setattr(loop, "emit_phase", lambda *_a, **_kw: None)
    monkeypatch.setattr(loop, "get_task_logger", lambda _: None)
    monkeypatch.setattr(loop, "is_no_test_project", lambda *_: False)
    monkeypatch.setattr(loop, "get_qa_iteration_count", lambda _: 0)
    monkeypatch.setattr(loop, "load_implementation_plan", lambda _: {})
    monkeypatch.setattr(loop, "save_implementation_plan", lambda *_: True)

    def unavailable(*_a, **_kw):
        raise IndependentReviewUnavailable("no second model")

    monkeypatch.setattr(loop, "pick_reviewer", unavailable)
    assert not await loop.run_qa_validation_loop(
        tmp_path, worktree, "sonnet", source_spec_dir=source, gates=gates()
    )
    assert json.loads((source / "qa_evidence.json").read_text()) == {
        "status": "unavailable",
        "reason": "no second model",
        "qa_cycle": 1,
    }


def test_no_alt_models_configured_falls_back_to_claude_ladder(monkeypatch):
    monkeypatch.delenv("AIFACTORY_QA_ALT_MODELS", raising=False)
    pick = pick_reviewer(
        gates("sonnet"),
        [identity("sonnet")],
        configured=lambda m: m in {"opus", "gpt-5.3-codex"},
    )
    assert pick.model == "opus"


def test_configured_and_approved_coding_models_outweigh_identity_file():
    configured = SimpleNamespace(
        model=lambda phase: {"qa": "sonnet", "coding": "sonnet", "qa_fixer": "opus"}[
            phase
        ],
        metadata={},
        approved_plan={"phases": [{"subtasks": [{"id": "1", "model": "fable"}]}]},
    )
    with pytest.raises(IndependentReviewUnavailable):
        pick_reviewer(
            configured,
            [identity("haiku")],
            configured=lambda model: model in {"sonnet", "opus", "fable"},
        )


def test_planning_identity_does_not_exclude_reviewer():
    selected = SimpleNamespace(
        model=lambda phase: "opus" if phase == "qa" else "sonnet", metadata={}
    )
    pick = pick_reviewer(
        selected,
        [identity("opus", phase="planning"), identity("sonnet")],
        configured=lambda model: model == "opus",
    )
    assert pick.model == "opus"
