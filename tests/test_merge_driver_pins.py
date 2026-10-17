"""Tests for pinning git merge drivers empty on server merges (#1761)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from core.child_env import merge_driver_pins

_WS = str(Path(__file__).resolve().parent.parent / "apps" / "web-server")
if _WS not in sys.path:
    sys.path.insert(0, _WS)

from server.services import pr_endgame  # noqa: E402


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def evil_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "pwned"

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")

    f = repo / "f"
    f.write_text("base\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "checkout", "-q", "-b", "aifactory/t")
    f.write_text("theirs\n")
    _git(repo, "commit", "-q", "-am", "theirs")

    _git(repo, "checkout", "-q", "main")
    f.write_text("ours\n")
    _git(repo, "commit", "-q", "-am", "ours")

    _git(repo, "config", "merge.evil.driver", f"touch {marker}; exit 0")
    info_attrs = repo / ".git" / "info" / "attributes"
    info_attrs.parent.mkdir(parents=True, exist_ok=True)
    info_attrs.write_text("* merge=evil\n")

    return repo, marker


def test_merge_driver_pins_blocks_merge_tree_driver(
    evil_repo: tuple[Path, Path],
) -> None:
    repo, marker = evil_repo

    control = subprocess.run(
        ["git", "merge-tree", "--write-tree", "main", "aifactory/t"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if not marker.exists():
        pytest.skip(
            f"git merge-tree did not run merge driver (rc={control.returncode})"
        )
    marker.unlink()

    pins = merge_driver_pins(repo)
    assert pins == ["-c", "merge.evil.driver="]

    pinned = subprocess.run(
        ["git", *pins, "merge-tree", "--write-tree", "main", "aifactory/t"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert pinned.returncode == 1
    assert not marker.exists()


def test_merge_driver_pins_injected_run(tmp_path: Path) -> None:
    assert merge_driver_pins(tmp_path, run=lambda _a: (1, "")) == []

    assert merge_driver_pins(
        tmp_path,
        run=lambda _a: (0, "merge.a.driver\0merge.B c.driver\0"),
    ) == ["-c", "merge.a.driver=", "-c", "merge.B c.driver="]

    with pytest.raises(RuntimeError):
        merge_driver_pins(tmp_path, run=lambda _a: (128, ""))

    with pytest.raises(RuntimeError):
        merge_driver_pins(tmp_path, run=lambda _a: (0, "merge.a=b.driver\0"))

    def _raise_oserror(_a: list[str]) -> tuple[int, str]:
        raise OSError("boom")

    with pytest.raises(RuntimeError):
        merge_driver_pins(tmp_path, run=_raise_oserror)


def test_backend_merge_tree_previews_pin_drivers_and_fail_closed(
    evil_repo: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli.workspace_commands as wc_mod
    import core.workspace.merge as merge_mod

    repo, marker = evil_repo

    res1 = merge_mod._check_git_conflicts(repo, "t")
    assert res1["has_conflicts"] is True
    assert not marker.exists()

    res2 = wc_mod._check_git_merge_conflicts(repo, "t")
    assert res2["has_conflicts"] is True
    assert not marker.exists()

    def _boom(_cwd: Path) -> list[str]:
        raise RuntimeError("broken config")

    monkeypatch.setattr(merge_mod, "merge_driver_pins", _boom)
    assert merge_mod._check_git_conflicts(repo, "t")["has_conflicts"] is True

    monkeypatch.setattr(wc_mod, "merge_driver_pins", _boom)
    assert wc_mod._check_git_merge_conflicts(repo, "t")["has_conflicts"] is True


def test_resolve_pr_conflicts_pins_merge_drivers(
    evil_repo: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, marker = evil_repo
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-q", "aifactory/t")
    _git(clone, "config", "merge.evil.driver", f"touch {marker}; exit 0")
    info_attrs = clone / ".git" / "info" / "attributes"
    info_attrs.parent.mkdir(parents=True, exist_ok=True)
    info_attrs.write_text("* merge=evil\n")

    seen: list[list[str]] = []

    def fixer(files: list[str], _wt: str) -> bool:
        seen.append(files)
        return False

    res = pr_endgame.resolve_pr_conflicts(
        str(clone),
        "main",
        fixer=fixer,
        runner=pr_endgame._default_runner,
    )
    assert res.resolved is False
    assert not marker.exists()
    assert seen == [["f"]]

    def _boom(_cwd: str, **_kw: object) -> list[str]:
        raise RuntimeError("broken config")

    monkeypatch.setattr(pr_endgame, "merge_driver_pins", _boom)
    refused = pr_endgame.resolve_pr_conflicts(
        str(clone),
        "main",
        fixer=fixer,
        runner=pr_endgame._default_runner,
    )
    assert refused.resolved is False
    assert "rebase refused" in refused.reason
