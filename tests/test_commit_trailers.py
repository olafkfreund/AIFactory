"""Agent commit trailers from requirements.json (Factory#1960)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from core.commit_trailers import (  # noqa: E402
    commit_message_args,
    commit_trailer_args,
    trailer_block,
)

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


def test_message_unchanged_when_off(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_AGENT_COMMIT_TRAILERS")
    msg = "x\n\nOriginator: mallory"
    assert commit_message_args(msg, _spec(tmp_path, FULL)) == ["-m", msg]


def test_message_collapsed_when_trailers_on(tmp_path):
    args = commit_message_args("x\n\nOriginator: mallory\r\x00", _spec(tmp_path, FULL))
    assert args[:2] == ["-m", "x Originator: mallory"]
    assert args[2:] == [t for v in TRAILERS.values() for t in v]


def test_trailer_block(tmp_path, monkeypatch):
    spec = _spec(tmp_path, FULL)
    assert (
        trailer_block(spec)
        == "Agent-Identity: coder-1\nAutonomy-Tier: medium\nOriginator: alice"
    )
    assert trailer_block(None) == ""
    _spec(tmp_path, {**FULL, "originator": None})
    assert trailer_block(spec) == "Agent-Identity: coder-1\nAutonomy-Tier: medium"
    monkeypatch.delenv("AIFACTORY_AGENT_COMMIT_TRAILERS")
    assert trailer_block(spec) == ""


# --- Plan-Slug (#2109) ---
from core.commit_trailers import stamp_plan_slug  # noqa: E402


def _args(tmp_path, **extra):
    return commit_trailer_args(_spec(tmp_path, {**FULL, **extra}))


def test_plan_slug_present_and_last(tmp_path):
    assert _args(tmp_path, plan_slug="owner/Repo#18")[-2:] == [
        "--trailer",
        "Plan-Slug: owner/Repo#18",
    ]


def test_plan_slug_absent_without_plan(tmp_path):
    assert not any("Plan-Slug" in a for a in _args(tmp_path))


@pytest.mark.parametrize("bad", [None, 5, "", "a b", "a\nb", "x" * 129])
def test_plan_slug_bad_omitted(tmp_path, bad):
    assert not any("Plan-Slug" in a for a in _args(tmp_path, plan_slug=bad))


def test_plan_slug_flag_off_and_not_duplicated(tmp_path, monkeypatch):
    spec = _spec(tmp_path, {**FULL, "plan_slug": "18"})
    block = trailer_block(spec)
    assert block.count("Plan-Slug:") == 1
    assert commit_message_args("m", spec).count("Plan-Slug: 18") == 1
    monkeypatch.delenv("AIFACTORY_AGENT_COMMIT_TRAILERS")
    assert commit_message_args("m", spec) == ["-m", "m"]


@pytest.mark.parametrize("corr", ["18", 18, "owner/Repo#18"])
def test_stamp_plan_slug(tmp_path, corr):
    spec = _spec(tmp_path, FULL)
    assert stamp_plan_slug(spec, corr)
    saved = json.loads((spec / "requirements.json").read_text())
    assert saved["plan_slug"] == str(corr) and saved["originator"] == "alice"


@pytest.mark.parametrize("corr", [None, True, "a b", ""])
def test_stamp_plan_slug_rejects(tmp_path, corr):
    spec = _spec(tmp_path, FULL)
    assert not stamp_plan_slug(spec, corr)
    assert "plan_slug" not in json.loads((spec / "requirements.json").read_text())
