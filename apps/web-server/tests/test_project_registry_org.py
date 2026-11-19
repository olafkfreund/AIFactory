"""Project paths select a single registered org without following symlinks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

WEB_SERVER = Path(__file__).resolve().parents[1]
if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))

from server import project_registry  # noqa: E402


def test_org_id_for_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    acme = tmp_path / "acme"
    globex = tmp_path / "globex"
    acme.mkdir()
    globex.mkdir()
    (acme / "link").symlink_to(globex, target_is_directory=True)
    entries = {
        "a": {"path": str(acme), "org_id": "acme"},
        "b": {"path": str(globex), "org_id": "globex"},
        "empty": {"path": "", "org_id": "globex"},
    }
    monkeypatch.setattr(project_registry, "load_projects", lambda: entries)
    lookup = project_registry.org_id_for_path
    assert lookup(acme) == "acme"
    assert lookup(acme / ".aifactory" / "worktrees" / "task") == "acme"
    assert lookup(acme / "link" / "file") == "acme"
    assert lookup(tmp_path / "missing") is None
    monkeypatch.chdir(acme)
    assert lookup(None) is None
    assert lookup("") is None
    assert lookup("relative") is None

    entries["same"] = {"path": str(acme / "nested"), "org_id": "acme"}
    assert lookup(acme / "nested" / "file") == "acme"
    entries["same"]["org_id"] = "globex"
    assert lookup(acme / "nested" / "file") is None
    entries["same"].pop("org_id")
    assert lookup(acme / "nested" / "file") is None

    entries.clear()
    assert lookup(acme) is None


def test_malformed_registry_entries_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme = tmp_path / "acme"
    entries: dict[str, Any] = {
        "x": "not-an-object",
        "y": {"path": 7},
        "a": {"path": str(acme), "org_id": "acme"},
    }
    monkeypatch.setattr(project_registry, "load_projects", lambda: entries)
    assert project_registry.org_id_for_path(acme) == "acme"
    entries["a"]["org_id"] = ["acme"]
    assert project_registry.org_id_for_path(acme) is None


def test_overlaps_other_org(tmp_path: Path) -> None:
    acme = tmp_path / "acme"
    projects = {
        "a": {"path": str(acme), "org_id": "acme"},
        "bad": {"path": 7, "org_id": "x"},
    }
    check = project_registry.overlaps_other_org
    assert check(acme / "sub", "globex", projects)
    assert check(tmp_path, "globex", projects)
    assert check(acme, "globex", projects)
    assert not check(acme / "sub", "acme", projects)
    assert not check(tmp_path / "other", "globex", projects)
    assert not check(acme, "globex", projects, skip="a")
    assert not check(tmp_path / "acme2", "globex", projects)
