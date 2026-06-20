"""Tests for the RFC-0012 house-standards prompt injection (get_house_standards_context)."""

from __future__ import annotations

import json
from pathlib import Path

from prompts_pkg.prompts import get_house_standards_context


def _write_contract(spec_dir: Path, contract: dict, *, where: str = "context/task_contract.json") -> None:
    path = spec_dir / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract))


def _standards(available: bool = True) -> dict:
    return {
        "epic_context": {
            "house_standards": {
                "available": available,
                "sources": [
                    {
                        "source": "baseline",
                        "kind": "conventions",
                        "conventions": {
                            "code_quality_tools": ["ruff", "mypy"],
                            "version_managers": ["uv"],
                        },
                        "content_hash": "sha256:abc",
                    },
                    {
                        "source": "backstage",
                        "kind": "component",
                        "entity_ref": "component:default/aifactory",
                        "techdocs_refs": ["dir:./docs/best-practices.md"],
                        "lifecycle": "production",
                        "content_hash": "sha256:def",
                    },
                ],
            }
        }
    }


def test_renders_block_with_tools_and_techdocs(tmp_path: Path):
    _write_contract(tmp_path, _standards())
    block = get_house_standards_context(tmp_path)
    assert "## HOUSE STANDARDS" in block
    assert "ruff, mypy" in block
    assert "uv" in block
    assert "production" in block
    assert "dir:./docs/best-practices.md" in block


def test_absent_contract_returns_empty(tmp_path: Path):
    assert get_house_standards_context(tmp_path) == ""


def test_unavailable_standards_returns_empty(tmp_path: Path):
    _write_contract(tmp_path, _standards(available=False))
    assert get_house_standards_context(tmp_path) == ""


def test_no_sources_returns_empty(tmp_path: Path):
    _write_contract(tmp_path, {"epic_context": {"house_standards": {"available": True, "sources": []}}})
    assert get_house_standards_context(tmp_path) == ""


def test_falls_back_to_implementation_plan(tmp_path: Path):
    _write_contract(tmp_path, _standards(), where="implementation_plan.json")
    block = get_house_standards_context(tmp_path)
    assert "ruff, mypy" in block


def test_malformed_json_never_raises(tmp_path: Path):
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "task_contract.json").write_text("{not json")
    assert get_house_standards_context(tmp_path) == ""
