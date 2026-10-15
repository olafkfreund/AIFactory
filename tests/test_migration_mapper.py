"""Tests for RFC-0010 Phase 7: AIFactory rewrite mode (migration_mapper)."""

from __future__ import annotations

from pathlib import Path

from core import migration_mapper as mm


def _contract(**over):
    c = {
        "change_mode": "migration",
        "environment": {
            "language": "rust",
            "source_language": "python",
            "target_language": "rust",
        },
        "tfactory": {
            "equivalence": {
                "module_map": {"pay/refund.py": "rust/port/src/pay/refund.rs"},
            }
        },
    }
    c.update(over)
    return c


# ── language resolution ─────────────────────────────────────────────────


def test_resolve_generation_language_migration():
    assert mm.resolve_generation_language(_contract()) == "rust"


def test_resolve_generation_language_non_migration_is_none():
    assert mm.resolve_generation_language({"change_mode": "modify"}) is None
    assert mm.resolve_generation_language(None) is None


def test_is_migration():
    assert mm.is_migration(_contract())
    assert not mm.is_migration({"change_mode": "modify"})


# ── module map + briefs ─────────────────────────────────────────────────


def test_module_map_from_contract():
    assert mm.module_map(_contract()) == {
        "pay/refund.py": "rust/port/src/pay/refund.rs"
    }


def test_module_briefs_attach_source(tmp_path: Path):
    oracle = tmp_path / "oracle"
    (oracle / "pay").mkdir(parents=True)
    (oracle / "pay" / "refund.py").write_text("def refund(a):\n    return a\n")
    briefs = mm.module_briefs(_contract(), oracle_root=oracle)
    assert len(briefs) == 1
    b = briefs[0]
    assert b.source_module == "pay/refund.py"
    assert b.target_module == "rust/port/src/pay/refund.rs"
    assert "def refund" in b.source_excerpt


# ── workspace prep: oracle mount + target scaffold ──────────────────────


def test_mount_oracle_copies_readonly_reference(tmp_path: Path):
    project = tmp_path / "proj"
    (project / "pay").mkdir(parents=True)
    (project / "pay" / "refund.py").write_text("def refund(a):\n    return a\n")
    (project / ".git").mkdir()
    (project / ".git" / "x").write_text("nope")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    oracle = mm.mount_oracle(worktree, project)
    assert (oracle / "pay" / "refund.py").is_file()
    assert (oracle / "README.READONLY").is_file()
    assert not (oracle / ".git").exists()  # .git excluded


def test_scaffold_target_creates_crate_and_stubs(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    created = mm.scaffold_target(worktree, _contract())
    cargo = worktree / "rust" / "port" / "Cargo.toml"
    stub = worktree / "rust" / "port" / "src" / "pay" / "refund.rs"
    assert cargo.is_file() and 'name = "port"' in cargo.read_text()
    assert stub.is_file() and "generate me" in stub.read_text()
    assert cargo in created and stub in created


def test_prepare_migration_workspace_end_to_end(tmp_path: Path):
    project = tmp_path / "proj"
    (project / "pay").mkdir(parents=True)
    (project / "pay" / "refund.py").write_text("def refund(a):\n    return a\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    summary = mm.prepare_migration_workspace(worktree, project, _contract())
    assert summary["target_language"] == "rust"
    assert (worktree / ".aifactory" / "oracle" / "pay" / "refund.py").is_file()
    assert (worktree / "rust" / "port" / "src" / "pay" / "refund.rs").is_file()
    assert summary["briefs"][0]["source_module"] == "pay/refund.py"


def test_prepare_is_noop_for_non_migration(tmp_path: Path):
    assert (
        mm.prepare_migration_workspace(tmp_path, tmp_path, {"change_mode": "modify"})
        == {}
    )


# ── coder brief + parity-harness scaffold (Gap 1b) ──────────────────────


def test_render_migration_brief_states_rules_and_modules():
    briefs = mm.module_briefs(_contract())
    text = mm.render_migration_brief(_contract(), briefs)
    assert "python -> rust" in text
    assert "read-only" in text and ".aifactory/oracle" in text
    assert "pay/refund.py` -> `rust/port/src/pay/refund.rs" in text


def test_scaffold_creates_parity_harness_stub(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    created = mm.scaffold_target(worktree, _contract())
    harness = worktree / "rust" / "port" / "src" / "bin" / "parity_harness.rs"
    assert harness.is_file() and "parity harness" in harness.read_text().lower()
    assert harness in created


def test_prepare_writes_brief_at_worktree_root(tmp_path: Path):
    project = tmp_path / "proj"
    (project / "pay").mkdir(parents=True)
    (project / "pay" / "refund.py").write_text("def refund(a):\n    return a\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    summary = mm.prepare_migration_workspace(worktree, project, _contract())
    brief = worktree / "MIGRATION_BRIEF.md"
    assert brief.is_file() and summary["brief"] == str(brief)
    assert "do **not** edit the legacy source" in brief.read_text().lower()


# ── #1673: migration acts only on a contract the server verified ────────

import contextlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from cli import build_commands  # noqa: E402
from core.contract_trust import ENV, contract_digest  # noqa: E402


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setenv("AIFACTORY_AUTH_PREFLIGHT", "off")


def _run_build(tmp_path: Path, contract: dict):
    """Drive handle_build_command; return (agent_mock, prepare_mock)."""
    spec_dir = tmp_path / "spec"
    (spec_dir / "context").mkdir(parents=True)
    (spec_dir / "context" / "task_contract.json").write_text(json.dumps(contract))
    review_state = MagicMock()
    review_state.is_approval_valid.return_value = True
    review_state_cls = MagicMock()
    review_state_cls.load.return_value = review_state
    agent = AsyncMock()
    prepare = MagicMock(return_value={})
    patches = [
        patch("agent.run_autonomous_agent", new=agent),
        patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
        patch("cli.utils.validate_environment", MagicMock(return_value=True)),
        patch("cli.utils.print_banner", MagicMock()),
        patch("core.migration_mapper.prepare_migration_workspace", prepare),
        patch.object(
            build_commands,
            "choose_workspace",
            MagicMock(return_value=build_commands.WorkspaceMode.ISOLATED),
        ),
        patch.object(
            build_commands, "get_existing_build_worktree", MagicMock(return_value=None)
        ),
        patch.object(
            build_commands,
            "setup_workspace",
            MagicMock(return_value=(tmp_path / "wt", None, spec_dir)),
        ),
        patch.object(build_commands, "ReviewState", review_state_cls),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        build_commands.handle_build_command(
            project_dir=tmp_path,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5",
            max_iterations=1,
            verbose=False,
            force_isolated=True,
            force_direct=False,
            auto_continue=True,
            skip_qa=True,
            force_bypass_approval=False,
            stop_after_planning=True,
        )
    return agent, prepare


def test_build_held_migration_exits_before_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ENV, "hold")
    with caplog.at_level(logging.WARNING, logger="cli.build_commands"):
        with pytest.raises(SystemExit) as exc:
            _run_build(tmp_path, _contract())
    assert exc.value.code == 1
    assert "[trusted-contract]" in caplog.text


def test_build_held_migration_never_calls_agent_or_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, "hold")
    agent = AsyncMock()
    prepare = MagicMock()
    with patch("agent.run_autonomous_agent", new=agent):
        with patch("core.migration_mapper.prepare_migration_workspace", prepare):
            with pytest.raises(SystemExit):
                _run_build(tmp_path, _contract())
    assert not agent.called
    assert not prepare.called


def test_build_held_non_migration_runs_without_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ENV, "hold")
    with caplog.at_level(logging.WARNING, logger="cli.build_commands"):
        agent, prepare = _run_build(tmp_path, {"feature": "f"})
    assert not prepare.called
    assert agent.call_count == 1
    assert "[trusted-contract] contract held" in caplog.text


def test_build_verified_migration_prepares_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, contract_digest(_contract()))
    _agent, prepare = _run_build(tmp_path, _contract())
    prepare.assert_called_once_with(tmp_path / "wt", tmp_path, _contract())


def test_build_legacy_migration_unchanged(tmp_path: Path) -> None:
    _agent, prepare = _run_build(tmp_path, _contract())
    assert prepare.call_count == 1
