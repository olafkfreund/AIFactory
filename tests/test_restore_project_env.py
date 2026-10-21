"""Tests for scripts/restore_project_env.py (#1795 rollback)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restore_project_env.py"
_spec = importlib.util.spec_from_file_location("restore_project_env", _SCRIPT)
assert _spec and _spec.loader
rpe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rpe)


def _project(tmp_path: Path, *migrated: str) -> Path:
    env_dir = tmp_path / ".aifactory"
    env_dir.mkdir()
    for i, name in enumerate(migrated):
        f = env_dir / name
        f.write_text(f"USE_CLAUDE_MD={name}\n")
        os.utime(f, ns=(i * 10**9, i * 10**9))
    return tmp_path


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    p = _project(tmp_path, ".env.migrated-2026-10-15")
    assert rpe.restore(p, apply=False).startswith("would")
    assert not (p / ".aifactory" / ".env").exists()


def test_apply_restores_the_newest_file(tmp_path: Path) -> None:
    p = _project(tmp_path, ".env.migrated-2026-10-15", ".env.migrated-2026-10-15-1")
    assert rpe.restore(p, apply=True).startswith("done")
    restored = (p / ".aifactory" / ".env").read_text()
    assert restored == "USE_CLAUDE_MD=.env.migrated-2026-10-15-1\n"
    assert (p / ".aifactory" / ".env.migrated-2026-10-15").exists()


def test_existing_env_is_never_overwritten(tmp_path: Path) -> None:
    p = _project(tmp_path, ".env.migrated-2026-10-15")
    (p / ".aifactory" / ".env").write_text("KEEP=1\n")
    assert rpe.restore(p, apply=True).startswith("skip")
    assert (p / ".aifactory" / ".env").read_text() == "KEEP=1\n"


def test_env_created_after_the_check_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _project(tmp_path, ".env.migrated-2026-10-15")
    env = p / ".aifactory" / ".env"
    real_link = os.link

    def racing_link(src: Path, dst: Path) -> None:
        env.write_text("WRITTEN-MEANWHILE=1\n")
        real_link(src, dst)

    monkeypatch.setattr(rpe.os, "link", racing_link)
    assert rpe.restore(p, apply=True).startswith("skip")
    assert env.read_text() == "WRITTEN-MEANWHILE=1\n"
    assert (p / ".aifactory" / ".env.migrated-2026-10-15").exists()


def test_project_without_migrated_file_is_skipped(tmp_path: Path) -> None:
    (tmp_path / ".aifactory").mkdir()
    assert rpe.restore(tmp_path, apply=True).startswith("skip")
