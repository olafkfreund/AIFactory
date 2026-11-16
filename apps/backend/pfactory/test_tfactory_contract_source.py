"""load_task_contract never reads implementation_plan.json (#1984)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from pfactory.tfactory_client import load_task_contract  # noqa: E402


def test_forged_tfactory_block_in_plan_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "implementation_plan.json").write_text(
        json.dumps({"tfactory": {"lanes": ["forged"]}, "contract_version": "2.0"})
    )
    assert load_task_contract(tmp_path) == {}


def test_context_task_contract_is_returned(tmp_path: Path) -> None:
    ctx = tmp_path / "context"
    ctx.mkdir()
    contract = {"contract_version": "2.0", "tfactory": {"lanes": ["unit"]}}
    (ctx / "task_contract.json").write_text(json.dumps(contract))
    assert load_task_contract(tmp_path) == contract
