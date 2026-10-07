"""Tests for scripts/gen_autonomy_matrix.py (Factory#1962)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_autonomy_matrix.py"
_spec = importlib.util.spec_from_file_location("gen_autonomy_matrix", _SCRIPT)
assert _spec and _spec.loader
gam = importlib.util.module_from_spec(_spec)
sys.modules["gen_autonomy_matrix"] = gam
_spec.loader.exec_module(gam)


def _label_of(entry: str) -> str:
    return gam._label(gam._entry_closure(entry)[1])


def test_markdown_contains_policy_constants() -> None:
    md = gam.render()
    for const in (gam.mp.AUTO_MERGE, gam.mp.HOLD_ASYNC, gam.mp.HOLD_BLOCKING):
        assert const in md


def test_labels() -> None:
    assert _label_of("merge.merge_policy") == "deterministic"
    assert _label_of("server.services.pr_review_service") == "model-assisted"


def test_empty_closure_is_below_minimum(tmp_path: Path) -> None:
    (tmp_path / "lonely.py").write_text("x = 1\n")
    local, names = gam._closure("lonely", (tmp_path,))
    assert len(local) == 1  # the entry only: nothing walked beyond it
    assert gam._label(names) == "deterministic"
    # An unresolvable entry yields an empty closure, which the minimum rejects.
    assert gam._closure("does.not.exist", (tmp_path,))[0] == set()


def test_below_minimum_exits_4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(gam._MIN_CLOSURE, "merge.merge_policy", 10_000)
    with pytest.raises(SystemExit) as e:
        gam._section_gates([])
    assert e.value.code == 4


def test_stale_spawn_edge_raises(tmp_path: Path) -> None:
    edge = gam._SPAWN_EDGES["server.services.pr_review_service"]
    caller = tmp_path / "caller.py"
    caller.write_text("import subprocess\nsubprocess.run(['x'])\n")
    with pytest.raises(ValueError, match="no longer matches"):
        gam._verify_spawn_edge(caller, edge)
    real = gam._REPO_ROOT / "apps/web-server/server/services/pr_review_service.py"
    gam._verify_spawn_edge(real, edge)  # the real caller still matches


def test_check_detects_tampering(tmp_path: Path) -> None:
    md, js = tmp_path / "m.md", tmp_path / "m.json"
    assert gam.main([], md_path=md, json_path=js) == 0
    assert gam.main(["--check"], md_path=md, json_path=js) == 0
    md.write_text(md.read_text().replace("hold-async", "hold-asynx", 1))
    assert gam.main(["--check"], md_path=md, json_path=js) == 1


def test_signature_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = gam.mp.decide_merge

    # Same parameter names as decide_merge plus one more.
    def widened(
        tier,
        *,
        host_ci_green,
        tfactory_verdict,
        achieved_val,
        val_floor,
        ci_parity,
        deployment=None,
        satisfied_gates=None,
        new_param=None,
    ):  # noqa: ANN001, ANN202
        return orig(tier)

    monkeypatch.setattr(gam.mp, "decide_merge", widened)
    with pytest.raises(SystemExit) as e:
        gam.render()
    assert e.value.code == 3
