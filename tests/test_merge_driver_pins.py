"""Tests for pinning git merge drivers empty on server merges (#1761)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from core.child_env import merge_driver_pins
from core.workspace.git_utils import has_uncommitted_changes
from core.worktree import WorktreeManager

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
    assert "merge.evil.driver=" in pins

    pinned = subprocess.run(
        ["git", *pins, "merge-tree", "--write-tree", "main", "aifactory/t"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert pinned.returncode == 1
    assert not marker.exists()


@pytest.fixture
def non_utf8_repo(
    evil_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """evil_repo with the bound driver renamed to the invalid-UTF-8 byte 0xff."""
    repo, marker = evil_repo
    monkeypatch.setenv("LC_ALL", "C.UTF-8")  # git's regex skips 0xff here
    with (repo / ".git" / "config").open("ab") as fh:
        fh.write(b'[merge "\xff"]\n\tdriver = touch ' + bytes(marker) + b"; exit 0\n")
    (repo / ".git" / "info" / "attributes").write_bytes(b"* merge=\xff\n")
    control = subprocess.run(
        ["git", "merge-tree", "--write-tree", "main", "aifactory/t"],
        cwd=repo,
        capture_output=True,
    )
    if not marker.exists():
        pytest.skip(f"git did not run the 0xff driver (rc={control.returncode})")
    marker.unlink()
    return repo, marker


def test_merge_driver_pins_non_utf8_subsection(
    non_utf8_repo: tuple[Path, Path],
) -> None:
    repo, marker = non_utf8_repo
    pins = merge_driver_pins(repo)
    assert "merge.\udcff.driver=" in pins

    pinned = subprocess.run(
        ["git", *pins, "merge-tree", "--write-tree", "main", "aifactory/t"],
        cwd=repo,
        capture_output=True,
    )
    assert pinned.returncode == 1
    assert not marker.exists()


@pytest.fixture
def filter_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "pwned"
    _git(repo, "init", "-q", "-b", "main")
    tracked = repo / "f"
    tracked.write_text("base\n")
    _git(repo, "add", "f")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "base",
    )

    _git(repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    (repo / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    tracked.write_text("edit\n")

    _git(repo, "status", "--porcelain")
    if not marker.exists():
        pytest.skip("git status did not run the clean filter")
    marker.unlink()
    return repo, marker


def test_merge_driver_pins_blocks_status_clean_filter(
    filter_repo: tuple[Path, Path],
) -> None:
    repo, marker = filter_repo

    pins = merge_driver_pins(repo)
    assert "filter.evil.clean=" in pins
    _git(repo, *pins, "status", "--porcelain")
    assert not marker.exists()


def test_merge_driver_pins_blocks_commit_clean_filter(
    filter_repo: tuple[Path, Path],
) -> None:
    """commit refreshes the index and re-runs clean on racy entries (#1832)."""
    repo, marker = filter_repo

    pins = merge_driver_pins(repo)
    _git(repo, *pins, "add", ".")
    ident = ["-c", "user.name=Test", "-c", "user.email=t@example.com"]
    _git(repo, *ident, *pins, "commit", "-q", "-m", "x")
    assert not marker.exists()


def test_backend_status_probes_pin_clean_filter(
    filter_repo: tuple[Path, Path],
) -> None:
    repo, marker = filter_repo
    assert WorktreeManager(repo, base_branch="main").has_uncommitted_changes() is True
    assert not marker.exists()
    assert has_uncommitted_changes(repo) is True
    assert not marker.exists()


def test_worktree_merge_pins_non_utf8_driver(
    non_utf8_repo: tuple[Path, Path],
) -> None:
    from core.worktree import WorktreeInfo, WorktreeManager

    repo, marker = non_utf8_repo
    mgr = WorktreeManager(repo, base_branch="main")
    info = WorktreeInfo(
        path=repo, branch="aifactory/t", spec_name="t", base_branch="main"
    )
    assert mgr._do_merge_locked("t", info, no_commit=False) is False
    assert not marker.exists()


def test_merge_driver_pins_injected_run(tmp_path: Path) -> None:
    def _script(driver_res: tuple[int, str]) -> object:
        # Answer the #1746 baseline git calls; script only the driver listing.
        return lambda a: driver_res if "--name-only" in a else (0, "")

    assert merge_driver_pins(tmp_path, run=_script((0, ""))) == []

    assert merge_driver_pins(
        tmp_path,
        run=_script(
            (
                0,
                "core.bare\0merge.a.driver\0merge.B c.driver\0merge.a.name\0"
                "filter.a.clean\0filter.a.smudge\0filter.a.process\0"
                "filter.a.required\0",
            )
        ),
    ) == [
        "-c",
        "merge.a.driver=",
        "-c",
        "merge.B c.driver=",
        "-c",
        "filter.a.clean=",
        "-c",
        "filter.a.smudge=",
        "-c",
        "filter.a.process=",
    ]

    with pytest.raises(RuntimeError):
        merge_driver_pins(tmp_path, run=_script((1, "")))

    with pytest.raises(RuntimeError):
        merge_driver_pins(tmp_path, run=_script((128, "")))

    with pytest.raises(RuntimeError):
        merge_driver_pins(tmp_path, run=_script((0, "merge.a=b.driver\0")))

    with pytest.raises(RuntimeError):
        merge_driver_pins(tmp_path, run=_script((0, "filter.a=b.clean\0")))

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


def _two_commit_clone(tmp_path: Path) -> Path:
    """Clone on aifactory/t whose rebase onto main conflicts on c1 (f) and
    then three-way merges g when it replays c2."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "Test")
    g_lines = [f"g{i}\n" for i in range(1, 10)]
    (origin / "f").write_text("base\n")
    (origin / "g").write_text("".join(g_lines))
    _git(origin, "add", "f", "g")
    _git(origin, "commit", "-q", "-m", "base")

    _git(origin, "checkout", "-q", "-b", "aifactory/t")
    (origin / "f").write_text("theirs\n")
    _git(origin, "commit", "-q", "-am", "c1")
    (origin / "g").write_text("".join(["G1\n", *g_lines[1:]]))
    _git(origin, "commit", "-q", "-am", "c2")

    _git(origin, "checkout", "-q", "main")
    (origin / "f").write_text("ours\n")
    (origin / "g").write_text("".join([*g_lines[:6], "G7\n", *g_lines[7:]]))
    _git(origin, "commit", "-q", "-am", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "checkout", "-q", "aifactory/t")
    return clone


def test_resolve_pr_conflicts_repins_after_fixer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A driver the fixer plants mid-rebase must not run on --continue."""
    clone = _two_commit_clone(tmp_path)
    marker = tmp_path / "late"

    def planting_fixer(_files: list[str], wt: str) -> bool:
        (Path(wt) / "f").write_text("fixed\n")
        _git(Path(wt), "config", "merge.late.driver", f"touch {marker}; exit 0")
        (Path(wt) / ".git" / "info").mkdir(exist_ok=True)
        (Path(wt) / ".git" / "info" / "attributes").write_text("* merge=late\n")
        return True

    res = pr_endgame.resolve_pr_conflicts(
        str(clone), "main", fixer=planting_fixer, runner=pr_endgame._default_runner
    )
    assert not marker.exists()
    # The pinned (empty) driver fails the g merge closed: rebase aborted.
    assert res.resolved is False
    assert not (clone / ".git" / "rebase-merge").exists()

    # A config the fixer breaks between the two scans aborts the rebase.
    (tmp_path / "second").mkdir()
    clone2 = _two_commit_clone(tmp_path / "second")
    real = pr_endgame.merge_driver_pins
    calls: list[str] = []

    def _second_boom(cwd: str, **kw: object) -> list[str]:
        calls.append(cwd)
        if len(calls) > 1:
            raise RuntimeError("broken config")
        return real(cwd, **kw)

    monkeypatch.setattr(pr_endgame, "merge_driver_pins", _second_boom)
    refused = pr_endgame.resolve_pr_conflicts(
        str(clone2), "main", fixer=planting_fixer, runner=pr_endgame._default_runner
    )
    assert refused.resolved is False
    assert "rebase refused" in refused.reason
    assert not (clone2 / ".git" / "rebase-merge").exists()


# --- #1832: server status/add/worktree diff must unpack the pins -----------

_ROOT = Path(__file__).resolve().parent.parent
_SERVER_GIT_SOURCES = (
    "apps/web-server/server",
    "apps/backend/core/worktree.py",
    "apps/backend/core/workspace",
    "apps/backend/cli/workspace_commands.py",
)


def _server_git_files() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for rel in _SERVER_GIT_SOURCES:
        p = _ROOT / rel
        for f in sorted(p.rglob("*.py")) if p.is_dir() else [p]:
            if "tests" in f.parts or f.name.startswith("test_"):
                continue
            out.append((f, f.relative_to(_ROOT).as_posix()))
    return out


def _callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id.lower()
    if isinstance(f, ast.Attribute):
        return f.attr.lower()
    return ""


def _needs_filter_pins(elts: list[ast.expr], wrapper: bool) -> bool:
    """True for a status/add/worktree-reading diff argv, or an unforced remove."""
    consts = [
        e.value
        for e in elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]
    if not consts:
        return False
    if consts[0] == "git":
        rest = consts[1:]
    elif wrapper:
        rest = consts
    else:
        return False
    rest = [v for v in rest if v not in ("-c",) and "=" not in v]
    if not rest:
        return False
    sub, args = rest[0], rest[1:]
    if sub in ("status", "add", "commit"):
        return True
    if sub == "worktree":
        return bool(args) and args[0] == "remove" and "--force" not in args
    if sub == "diff":
        non_const = [
            e
            for e in elts
            if not (isinstance(e, ast.Constant) and isinstance(e.value, str))
            and not isinstance(e, ast.Starred)
        ]
        return (
            not non_const
            and all(a.startswith("-") for a in args)
            and not {"--cached", "--staged"} & set(args)
        )
    return False


def _unpinned_filter_sites(tree: ast.AST, rel: str) -> list[str]:
    pin_names = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and _callee_name(n.value) == "merge_driver_pins"
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    wrapper_args: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and "git" in _callee_name(n) and n.args:
            wrapper_args.add(id(n.args[0]))
    hits: list[str] = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.List, ast.Tuple)):
            continue
        if not _needs_filter_pins(n.elts, id(n) in wrapper_args):
            continue
        pinned = any(
            isinstance(e, ast.Starred)
            and (
                (isinstance(e.value, ast.Name) and e.value.id in pin_names)
                or (
                    isinstance(e.value, ast.Call)
                    and _callee_name(e.value) == "merge_driver_pins"
                )
            )
            for e in n.elts
        )
        if not pinned:
            hits.append(f"{rel}:{n.lineno}")
    return hits


def test_scan_flags_unpinned_filter_sites() -> None:
    def hits(src: str) -> list[str]:
        return _unpinned_filter_sites(ast.parse(src), "x.py")

    assert hits('["git", "status", "--porcelain"]') == ["x.py:1"]
    assert hits('run_git_command(["add", "-A"], p)') == ["x.py:1"]
    assert hits('["git", "diff", "--name-only", "--diff-filter=U"]') == ["x.py:1"]
    assert hits('["git", "commit", "-m", m]') == ["x.py:1"]
    assert hits('["git", "worktree", "remove", p]') == ["x.py:1"]
    assert hits('pins = merge_driver_pins(p)\n["git", *pins, "add", f]\n') == []
    assert hits('["git", "diff", "--name-only", f"{a}...{b}"]') == []
    assert hits('["git", "diff", "--cached", "--name-only"]') == []
    assert hits('["git", "worktree", "add", p]') == []
    assert hits('["git", "worktree", "remove", "--force", p]') == []
    assert hits('["git", "remote", "add", "origin", u]') == []


def test_every_server_status_add_diff_pins_filters() -> None:
    """Server git calls that read the worktree must unpack the pins (#1832).

    Known limit: only literal argv is seen, like the #1718/#1761 scans.
    """
    offenders = [
        hit
        for path, rel in _server_git_files()
        for hit in _unpinned_filter_sites(
            ast.parse(path.read_text(encoding="utf-8"), filename=rel), rel
        )
    ]
    assert not offenders, (
        "server git status/add/commit/worktree diff missing *merge_driver_pins (#1832):\n"
        + "\n".join(offenders)
    )


def test_terminal_worktree_remove_pins_per_worktree_filter(tmp_path: Path) -> None:
    """Unforced `worktree remove` runs status in the worktree, whose
    config.worktree can define a filter the main repo's config does not list."""
    import json

    from server.services.terminal_worktree_service import TerminalWorktreeService

    repo = tmp_path / "repo"
    repo.mkdir()
    wt = tmp_path / "wt"
    marker = tmp_path / "pwned"
    ident = ["-c", "user.name=Test", "-c", "user.email=t@example.com"]
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("base\n")
    _git(repo, "add", "f")
    _git(repo, *ident, "commit", "-q", "-m", "base")
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(wt, "config", "--worktree", "filter.evil.clean", f"touch {marker}; cat")
    (repo / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    (wt / "f").write_text("edit\n")
    _git(wt, "status", "--porcelain")
    if not marker.exists():
        pytest.skip("git status did not run the per-worktree clean filter")
    marker.unlink()

    svc = object.__new__(TerminalWorktreeService)
    svc.project_path = repo
    svc.config_file = repo / ".aifactory" / "terminal-worktrees.json"
    svc.config_file.parent.mkdir()
    svc.config_file.write_text(
        json.dumps({"worktrees": [{"name": "wt", "path": str(wt), "branch": "wt"}]})
    )

    svc.remove_worktree("wt")
    assert not marker.exists()
