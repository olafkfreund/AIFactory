"""Agent commit trailers from requirements.json (Factory#1960)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core.commit_trailers import commit_trailer_args  # noqa: E402

FULL = {
    "agent_identity": "coder-1",
    "originator": "alice",
    "intake": {"autonomy_tier": "medium"},
}
TRAILERS = {
    "agent_identity": ["--trailer", "Agent-Identity: coder-1"],
    "tier": ["--trailer", "Autonomy-Tier: medium"],
    "originator": ["--trailer", "Originator: alice"],
}


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv("AIFACTORY_AGENT_COMMIT_TRAILERS", "1")


def _spec(tmp_path: Path, data) -> Path:
    (tmp_path / "requirements.json").write_text(json.dumps(data))
    return tmp_path


def test_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_AGENT_COMMIT_TRAILERS")
    assert commit_trailer_args(_spec(tmp_path, FULL)) == []


def test_all_three_in_order(tmp_path):
    assert commit_trailer_args(_spec(tmp_path, FULL)) == [
        "--trailer",
        "Agent-Identity: coder-1",
        "--trailer",
        "Autonomy-Tier: medium",
        "--trailer",
        "Originator: alice",
    ]


@pytest.mark.parametrize("bad", [None, 5, "", "a\nb", "a\rb", "a\x00b", "MISSING"])
@pytest.mark.parametrize("field", ["agent_identity", "tier", "originator"])
def test_bad_value_omits_only_that_trailer(tmp_path, field, bad):
    data = json.loads(json.dumps(FULL))
    target = data["intake"] if field == "tier" else data
    key = "autonomy_tier" if field == "tier" else field
    if bad == "MISSING":
        del target[key]
    else:
        target[key] = bad
    expected = [a for f, t in TRAILERS.items() if f != field for a in t]
    assert commit_trailer_args(_spec(tmp_path, data)) == expected


def test_unusable_inputs(tmp_path):
    assert commit_trailer_args(None) == []
    assert commit_trailer_args(tmp_path) == []  # missing file
    (tmp_path / "requirements.json").write_text("{nope")
    assert commit_trailer_args(tmp_path) == []
    assert commit_trailer_args(_spec(tmp_path, [1, 2])) == []
