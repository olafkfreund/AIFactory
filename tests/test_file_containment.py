"""Path-containment tests for the absolute-path file routes (epic #318, #320).

Proves arbitrary host read/list/serve is blocked (only registered projects /
browsable roots), `~` isn't expanded to escape, `/serve` rejects an
attacker-controlled root, and writes/deletes can't touch `.git/`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes import files as files_mod  # noqa: E402
from server.routes.files import (  # noqa: E402
    FileWrite,
    delete_file,
    discover_projects,
    list_directory_direct,
    read_file_direct,
    serve_project_file,
    write_file,
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.py").write_text("print(1)")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("[core]\n")
    monkeypatch.setattr(
        "server.project_registry.load_projects", lambda: {"p": {"path": str(proj)}}
    )
    return proj


# ── /read — content read confined to registered projects ───────────────────


async def test_read_inside_project_ok(project):
    res = await read_file_direct(path=str(project / "src" / "a.py"))
    assert res["content"] == "print(1)"


async def test_read_etc_passwd_is_403(project):
    with pytest.raises(HTTPException) as exc:
        await read_file_direct(path="/etc/passwd")
    assert exc.value.status_code == 403


async def test_read_tilde_not_expanded_is_403(project):
    # `~` must NOT expand — `~/.aws/credentials` resolves to a literal, outside
    # any project → 403 (was readable before #320).
    with pytest.raises(HTTPException) as exc:
        await read_file_direct(path="~/.aws/credentials")
    assert exc.value.status_code == 403


async def test_read_traversal_out_of_project_is_403(project):
    with pytest.raises(HTTPException) as exc:
        await read_file_direct(
            path=str(project / "src" / ".." / ".." / ".." / "etc" / "passwd")
        )
    assert exc.value.status_code == 403


# ── /list + /discover — browse confined to browsable roots ─────────────────


async def test_list_system_dir_is_403(project):
    with pytest.raises(HTTPException) as exc:
        await list_directory_direct(path="/etc")
    assert exc.value.status_code == 403


async def test_list_inside_project_ok(project):
    res = await list_directory_direct(path=str(project / "src"))
    assert any(e["name"] == "a.py" for e in res["entries"])


async def test_discover_system_dir_is_403(project):
    with pytest.raises(HTTPException) as exc:
        await discover_projects(base_path="/etc")
    assert exc.value.status_code == 403


# ── /serve — attacker-controlled root rejected ─────────────────────────────


async def test_serve_root_traversal_is_403(project, monkeypatch):
    monkeypatch.setattr(files_mod, "_validate_serve_token", lambda *a, **k: True)
    req = SimpleNamespace(headers={}, query_params={})
    with pytest.raises(HTTPException) as exc:
        await serve_project_file(request=req, path="/etc/passwd", root="/", token="")
    assert exc.value.status_code == 403


# ── /write + /delete — .git is off-limits ──────────────────────────────────


async def test_write_into_git_is_403(project):
    with pytest.raises(HTTPException) as exc:
        await write_file(
            project_id="p",
            path=".git/hooks/pre-commit",
            file_data=FileWrite(content="x"),
        )
    assert exc.value.status_code == 403


async def test_delete_inside_git_is_403(project):
    with pytest.raises(HTTPException) as exc:
        await delete_file(project_id="p", path=".git/config")
    assert exc.value.status_code == 403


async def test_write_normal_file_ok(project):
    res = await write_file(
        project_id="p", path="src/new.py", file_data=FileWrite(content="x=1")
    )
    assert res["success"] is True
    assert (project / "src" / "new.py").read_text() == "x=1"
