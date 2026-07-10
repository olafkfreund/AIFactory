#!/usr/bin/env python3
"""
Tests for the RFC-0015 §3.1 per-project constitution prompt injection.

The AIFactory coder / QA agents honour the project's human-authored governing
principles (``epic_context.constitution``) the SAME way they honour RFC-0012
house standards — ``get_constitution_context`` renders a prompt block, wired
into the solo, coder and QA prompt builders.

Coverage (mirrors tests/test_solo_mode.py + the house-standards test):
- Unit: present -> principles rendered, enforceable clauses marked HARD;
  absent / unavailable / no-principles / malformed -> "" (no change).
- Integration: a built solo prompt carries the constitution block when the
  contract supplies one, and does not when it is absent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from prompts_pkg.prompts import get_constitution_context  # noqa: E402


def _write_contract(
    spec_dir: Path, contract: dict, *, where: str = "context/task_contract.json"
) -> None:
    path = spec_dir / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract))


def _constitution(available: bool = True) -> dict:
    return {
        "epic_context": {
            "constitution": {
                "available": available,
                "source": ".factory/constitution.md",
                "principles": [
                    {
                        "id": "P1",
                        "text": "All code must have unit tests before merge.",
                        "enforceable": True,
                    },
                    {
                        "id": "P2",
                        "text": "Prefer composition over inheritance.",
                        "enforceable": False,
                    },
                ],
                "enforceable_ids": ["P1"],
            }
        }
    }


# --- unit: get_constitution_context -----------------------------------------


def test_renders_block_with_principles(tmp_path: Path):
    _write_contract(tmp_path, _constitution())
    block = get_constitution_context(tmp_path)
    assert "## PROJECT CONSTITUTION" in block
    assert ".factory/constitution.md" in block
    assert "All code must have unit tests before merge." in block
    assert "Prefer composition over inheritance." in block


def test_enforceable_marked_hard(tmp_path: Path):
    _write_contract(tmp_path, _constitution())
    block = get_constitution_context(tmp_path)
    # P1 (enforceable) -> rendered as a HARD REQUIREMENT.
    assert "HARD REQUIREMENT" in block
    hard_lines = [
        ln for ln in block.splitlines() if "HARD REQUIREMENT" in ln and "[P1]" in ln
    ]
    assert hard_lines, "enforceable principle P1 should be a hard requirement"
    # P2 (advisory) -> NOT a hard requirement.
    advisory_lines = [ln for ln in block.splitlines() if "[P2]" in ln]
    assert advisory_lines
    assert all("HARD REQUIREMENT" not in ln for ln in advisory_lines)


def test_enforceable_via_top_level_ids(tmp_path: Path):
    # A principle flagged only through enforceable_ids (not its own flag) is hard.
    contract = {
        "epic_context": {
            "constitution": {
                "available": True,
                "source": "backstage",
                "principles": [
                    {
                        "id": "P9",
                        "text": "Secrets never live in source.",
                        "enforceable": False,
                    },
                ],
                "enforceable_ids": ["P9"],
            }
        }
    }
    _write_contract(tmp_path, contract)
    block = get_constitution_context(tmp_path)
    assert "HARD REQUIREMENT" in block
    assert "Secrets never live in source." in block


def test_absent_contract_returns_empty(tmp_path: Path):
    assert get_constitution_context(tmp_path) == ""


def test_no_constitution_block_returns_empty(tmp_path: Path):
    _write_contract(
        tmp_path, {"epic_context": {"house_standards": {"available": True}}}
    )
    assert get_constitution_context(tmp_path) == ""


def test_unavailable_constitution_returns_empty(tmp_path: Path):
    _write_contract(tmp_path, _constitution(available=False))
    assert get_constitution_context(tmp_path) == ""


def test_no_principles_returns_empty(tmp_path: Path):
    _write_contract(
        tmp_path,
        {
            "epic_context": {
                "constitution": {
                    "available": True,
                    "principles": [],
                    "enforceable_ids": [],
                }
            }
        },
    )
    assert get_constitution_context(tmp_path) == ""


def test_falls_back_to_implementation_plan(tmp_path: Path):
    _write_contract(tmp_path, _constitution(), where="implementation_plan.json")
    block = get_constitution_context(tmp_path)
    assert "All code must have unit tests before merge." in block


def test_malformed_json_never_raises(tmp_path: Path):
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "task_contract.json").write_text("{not json")
    assert get_constitution_context(tmp_path) == ""


# --- integration: built prompt carries the constitution ---------------------


def test_solo_prompt_includes_constitution_when_present(tmp_path: Path):
    from prompts import get_solo_prompt

    (tmp_path / "spec.md").write_text("# Feature\nDo a small thing.\n")
    _write_contract(tmp_path, _constitution())

    prompt = get_solo_prompt(tmp_path)
    assert "## PROJECT CONSTITUTION" in prompt
    assert "All code must have unit tests before merge." in prompt
    assert "HARD REQUIREMENT" in prompt


def test_solo_prompt_unchanged_when_constitution_absent(tmp_path: Path):
    from prompts import get_solo_prompt

    (tmp_path / "spec.md").write_text("# Feature\nDo a small thing.\n")
    # No contract at all -> no constitution block injected.
    prompt = get_solo_prompt(tmp_path)
    assert "## PROJECT CONSTITUTION" not in prompt
