#!/usr/bin/env python3
"""
Skills cache is JSON, not pickle (#324 L1 — epic #318)
======================================================

A server-generated cache that is ``pickle.load``-ed is an RCE primitive if its
path ever becomes writable/traversable. These tests pin the JSON cache:
the on-disk file is valid JSON (inert), the round-trip reconstructs the
dataclasses, and the cache is written 0600.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))

from server.services.skills_service import (  # noqa: E402
    _CACHE_VERSION,
    SkillsService,
)


@pytest.fixture
def skills_tree(tmp_path) -> Path:
    base = tmp_path / "skills"
    cat = base / "lang"
    cat.mkdir(parents=True)
    (cat / "python.md").write_text("> Source: docs\n\nPython programming language.\n")
    (cat / "rust.md").write_text("> Source: docs\n\nRust systems language.\n")
    return base


def test_cache_file_is_inert_json(skills_tree, tmp_path):
    cache = tmp_path / "skills-cache.json"
    svc = SkillsService(skills_base_path=skills_tree, cache_path=cache)
    svc.build_index()

    assert cache.exists()
    data = json.loads(cache.read_text())  # must parse as JSON, not pickle
    assert data["version"] == _CACHE_VERSION
    assert "lang" in data["index"]


def test_cache_is_owner_only(skills_tree, tmp_path):
    cache = tmp_path / "skills-cache.json"
    SkillsService(skills_base_path=skills_tree, cache_path=cache).build_index()
    assert oct(cache.stat().st_mode)[-3:] == "600"


def test_round_trip_reconstructs_dataclasses(skills_tree, tmp_path):
    cache = tmp_path / "skills-cache.json"
    SkillsService(skills_base_path=skills_tree, cache_path=cache).build_index()

    # A second instance loads from the JSON cache.
    svc2 = SkillsService(skills_base_path=skills_tree, cache_path=cache)
    assert svc2._load_cache() is True

    entries = svc2._index["lang"]
    assert sorted(e.summary.name for e in entries) == ["python", "rust"]
    entry = entries[0]
    assert entry.summary.id.startswith("lang/")
    assert isinstance(entry.name_tokens, frozenset)
    assert isinstance(entry.file_path, Path)


def test_foreign_version_cache_is_rejected_and_rebuilt(skills_tree, tmp_path):
    cache = tmp_path / "skills-cache.json"
    # A cache with a mismatched version must never be trusted: constructing the
    # service rebuilds the index and overwrites it with the current version.
    cache.write_text(json.dumps({"version": 999, "base_path": str(skills_tree), "index": {}}))
    SkillsService(skills_base_path=skills_tree, cache_path=cache)

    rebuilt = json.loads(cache.read_text())
    assert rebuilt["version"] == _CACHE_VERSION
    assert "lang" in rebuilt["index"]  # real scan output, not the empty foreign index


def test_load_cache_rejects_foreign_version_directly(skills_tree, tmp_path):
    # Unit check on the guard itself: a freshly-written foreign cache that is
    # newer than the skills tree is still rejected on its version.
    cache = tmp_path / "skills-cache.json"
    svc = SkillsService(skills_base_path=skills_tree, cache_path=cache)  # writes a valid v2 cache
    cache.write_text(json.dumps({"version": 999, "base_path": str(skills_tree), "index": {}}))
    assert svc._load_cache() is False
