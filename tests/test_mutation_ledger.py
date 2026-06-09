"""Tests for checkpoint-before-mutation + the mutation ledger (#476)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents.mutation_ledger import (
    MutationLedger,
    git_checkpoint,
    mutation_target,
    rollback_to,
    verify_turn,
)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("print(1)\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


# ── ledger ───────────────────────────────────────────────────────────────────


def test_record_and_read_roundtrip(tmp_path: Path):
    led = MutationLedger(tmp_path)
    led.record(tool="Write", target="a.py", ok=True, tool_use_id="t1")
    led.record(tool="Bash", target="rm x", ok=False, tool_use_id="t2")
    rows = led.read()
    assert [r["tool"] for r in rows] == ["Write", "Bash"]
    assert rows[0]["target"] == "a.py" and rows[0]["ok"] is True
    assert (tmp_path / ".aifactory" / "mutations.jsonl").exists()


def test_mutation_target_extraction():
    assert mutation_target("Write", {"file_path": "x.py"}) == "x.py"
    assert mutation_target("Edit", {"path": "y.ts"}) == "y.ts"
    assert mutation_target("Bash", {"command": "rm -rf build"}).startswith(
        "rm -rf build"
    )
    assert mutation_target("Read", {"file_path": "z"}) == "z"
    assert mutation_target("Bash", "not-a-dict") is None


def test_claimed_targets_only_file_mutations(tmp_path: Path):
    led = MutationLedger(tmp_path)
    led.record(tool="Write", target="a.py", ok=True)
    led.record(tool="Bash", target="echo hi", ok=True)  # not a file mutation
    assert led.claimed_targets() == {"a.py"}


# ── checkpoint + rollback ────────────────────────────────────────────────────


def test_checkpoint_none_when_clean(repo: Path):
    assert git_checkpoint(repo) is None  # nothing to snapshot


def test_checkpoint_and_rollback_restores_tree(repo: Path):
    (repo / "a.py").write_text("print(999)\n")  # dirty the tree
    cp = git_checkpoint(repo)
    assert cp  # got a snapshot sha
    (repo / "a.py").write_text("CORRUPTED\n")  # a bad mutation
    assert rollback_to(repo, cp) is True
    assert (repo / "a.py").read_text() == "print(999)\n"  # restored to checkpoint


# ── turn-end verification ────────────────────────────────────────────────────


def test_verify_turn_ok_when_claims_match_disk(repo: Path):
    (repo / "a.py").write_text("changed\n")  # real change
    led = MutationLedger(repo)
    led.record(tool="Write", target="a.py", ok=True)
    res = verify_turn(repo, repo)
    assert res["ok"] is True
    assert "a.py" in res["claimed"] and "a.py" in res["actual"]
    assert res["missing"] == []


def test_verify_turn_flags_claimed_but_unchanged(repo: Path):
    led = MutationLedger(repo)
    led.record(tool="Write", target="ghost.py", ok=True)  # claimed but never written
    res = verify_turn(repo, repo)
    assert res["ok"] is False
    assert "ghost.py" in res["missing"]


def test_verify_turn_reports_unexpected_drift(repo: Path):
    (repo / "surprise.py").write_text("x\n")  # changed, not in ledger
    res = verify_turn(repo, repo)
    assert "surprise.py" in res["unexpected"]
