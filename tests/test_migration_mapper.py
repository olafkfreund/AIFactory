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
