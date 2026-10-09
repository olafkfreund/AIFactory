"""Tests for scripts/gen_autonomy_matrix.py (Factory#1962)."""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from collections.abc import Callable
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


def test_tier_rows_contain_policy_constants() -> None:
    tabs: list[dict[str, object]] = []
    gam._section_tiers(tabs)
    cells = {c for row in tabs[0]["rows"] for c in row}  # type: ignore[attr-defined]
    for const in (gam.mp.AUTO_MERGE, gam.mp.HOLD_ASYNC, gam.mp.HOLD_BLOCKING):
        assert f"`{const}`" in cells


def test_labels() -> None:
    assert _label_of("merge.merge_policy") == "deterministic"
    assert _label_of("server.services.pr_review_service") == "model-assisted"


def test_unresolvable_entry_is_empty(tmp_path: Path) -> None:
    assert gam._closure("does.not.exist", (tmp_path,))[0] == frozenset()


def test_closure_below_minimum_refuses_to_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tiny = (frozenset({"merge.merge_policy"}), frozenset(), "-", (), {})
    monkeypatch.setattr(gam, "_entry_closure", lambda _entry: tiny)
    monkeypatch.setitem(gam._MIN_CLOSURE, "merge.merge_policy", 2)
    with pytest.raises(SystemExit) as e:
        gam._section_gates([])
    assert e.value.code == 4
    out = capsys.readouterr().out
    assert "refusing to label" in out and "deterministic" not in out


def test_below_minimum_exits_4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(gam._MIN_CLOSURE, "merge.merge_policy", 10_000)
    with pytest.raises(SystemExit) as e:
        gam._section_gates([])
    assert e.value.code == 4


_EDGE = {
    "script": "apps/backend/runners/github/runner.py",
    "extra_roots": ["apps/backend/runners/github"],
}


def _caller(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "caller.py"
    f.write_text(body)
    return f


_GOOD = """
import asyncio
async def go():
    pp = base / "runners" / "github"
    script = base / "runners" / "github" / "runner.py"
    cmd = [str(script)]
    await asyncio.create_subprocess_exec(*cmd)
"""


def test_spawn_edge_accepts_good_shape(tmp_path: Path) -> None:
    gam._verify_spawn_edge(_caller(tmp_path, _GOOD), _EDGE)
    real = gam._REPO_ROOT / "apps/web-server/server/services/pr_review_service.py"
    gam._verify_spawn_edge(real, _EDGE)


@pytest.mark.parametrize(
    "body",
    [
        # strings present, no spawn call
        'pp = b / "runners" / "github"\nscript = b / "runners" / "github" / "runner.py"\n',
        # spawn present, script path split across statements
        'pp = b / "runners" / "github"\nx = "runners"\ny = "github"\n'
        'z = "runner.py"\nsubprocess.run([x, y, z])\n',
        # spawn present but never uses the script variable
        'pp = b / "runners" / "github"\nscript = b / "runners" / "github" / "runner.py"\n'
        "subprocess.run(['ls'])\n",
    ],
)
def test_stale_spawn_edge_raises(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match="no longer matches"):
        gam._verify_spawn_edge(_caller(tmp_path, body), _EDGE)


def _pkg(tmp_path: Path) -> tuple[Path, ...]:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import anthropic\n")
    (pkg / "a.py").write_text("from . import b\nfrom .c import d\n")
    (pkg / "b.py").write_text("X = 1\n")
    (pkg / "c.py").write_text("d = 1\n")
    (pkg / "e.py").write_text("from .c import d\n")
    (pkg / "dyn.py").write_text("import importlib\nimportlib.import_module('x')\n")
    return (tmp_path,)


def test_prober_resolves_relative_imports(tmp_path: Path) -> None:
    roots = _pkg(tmp_path)
    local, _names, dynamic = gam._closure("pkg.a", roots)
    assert {"pkg.a", "pkg.b", "pkg.c"} <= local
    assert dynamic == ()


def test_dynamic_import_is_undetermined(tmp_path: Path) -> None:
    roots = _pkg(tmp_path)
    _local, names, dynamic = gam._closure("pkg.dyn", roots)
    assert dynamic == ("pkg.dyn:2",)
    assert gam._label(names, dynamic).startswith(
        "undetermined (dynamic import: pkg.dyn:2"
    )


def test_parent_init_reported_separately(tmp_path: Path) -> None:
    roots = _pkg(tmp_path)
    local, names, _ = gam._closure("pkg.e", roots)
    assert gam._label(names) == "deterministic"  # the module's own imports
    reach = gam._parent_reach(local, roots)
    assert any(
        k.endswith("__init__.py") and v == ["anthropic"] for k, v in reach.items()
    )


def test_merge_policy_parent_init_reaches_model_client() -> None:
    _, _, _, _, reach = gam._entry_closure("merge.merge_policy")
    assert any(k.endswith("merge/__init__.py") for k in reach)


def test_check_detects_tampering(tmp_path: Path) -> None:
    md, js = tmp_path / "m.md", tmp_path / "m.json"
    assert gam.main([], md_path=md, json_path=js) == 0
    assert gam.main(["--check"], md_path=md, json_path=js) == 0
    md.write_text(md.read_text().replace("hold-async", "hold-asynx", 1))
    assert gam.main(["--check"], md_path=md, json_path=js) == 1


def test_signature_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real = inspect.signature(gam.mp.decide_merge)
    extra = inspect.Parameter("new_param", inspect.Parameter.KEYWORD_ONLY, default=None)

    def widened(*_args: object, **_kwargs: object) -> str:
        return gam.mp.HOLD_BLOCKING

    # Report decide_merge's real signature plus one more keyword.
    widened.__signature__ = real.replace(  # type: ignore[attr-defined]
        parameters=[*real.parameters.values(), extra]
    )
    monkeypatch.setattr(gam.mp, "decide_merge", widened)
    with pytest.raises(SystemExit) as e:
        gam.render()
    assert e.value.code == 3


def _toml_copy(tmp_path: Path, edit: Callable[[str], str]) -> Path:
    out = tmp_path / "controls.toml"
    out.write_text(edit(gam._CONTROLS_TOML.read_text(encoding="utf-8")))
    return out


def _run_check(tmp_path: Path, toml: Path) -> int:
    md, js = tmp_path / "m.md", tmp_path / "m.json"
    assert gam.main([], md_path=md, json_path=js) == 0
    return gam.main(["--check"], md_path=md, json_path=js, controls_path=toml)


def test_orphan_toml_entry_fails_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _toml_copy(
        tmp_path,
        lambda t: t + '\n[controls."bogus.control"]\nobjective = "x"\n'
        'evidence = "y"\nframeworks = []\n',
    )
    assert _run_check(tmp_path, toml) == 1
    assert "orphan control (no derived id): bogus.control" in capsys.readouterr().out


def test_derived_control_without_entry_fails_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _toml_copy(
        tmp_path, lambda t: t.replace("policy.val_floor", "policy.val_floor_x")
    )
    assert _run_check(tmp_path, toml) == 1
    out = capsys.readouterr().out
    assert "derived control has no entry: policy.val_floor" in out
    assert "orphan control (no derived id): policy.val_floor_x" in out


def test_malformed_control_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _toml_copy(
        tmp_path, lambda t: t.replace("frameworks = [", "frameworkz = [", 1)
    )
    assert _run_check(tmp_path, toml) == 1
    assert "malformed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        # A non-table entry must be a named error, not an AttributeError traceback.
        (
            lambda t: t + '\n[controls]\nstray = "value"\n',
            "control 'stray' is malformed",
        ),
        # `claim` is published verbatim, so it must be text.
        (
            lambda t: re.sub(r'^claim = ".*"$', "claim = 1", t, count=1, flags=re.M),
            "control 'policy.tier.low' is malformed",
        ),
    ],
)
def test_malformed_shapes_are_named_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    edit: Callable[[str], str],
    message: str,
) -> None:
    toml = _toml_copy(tmp_path, edit)
    assert _run_check(tmp_path, toml) == 1
    assert message in capsys.readouterr().out


def test_live_overlay_rows_match_the_live_code(tmp_path: Path) -> None:
    from cli import workspace_commands as wc  # noqa: PLC0415

    rows = gam._live_overlay_rows(wc, tmp_path)
    assert len(rows) == 4  # two deployment probes x enforce unset / "1"
    unset = [r for r in rows if r[1] == "unset"]
    assert unset
    # Recompute each row from its own probe directory (it holds the contract).
    for i, r in enumerate(rows):
        tier = r[2].strip("`").strip("'")
        val = None if r[1] == "unset" else "1"
        with gam._env(**{gam.pe.PATH_RISK_FLOOR_ENV: val}, **gam._ISOLATED_HOST):
            live = gam.pe.merge_disposition(tmp_path / f"live-{i}", tier, trusted=None)
        assert r[4] == f"`{live}`"


def test_live_overlay_sentence_tracks_unset_auto_merge() -> None:
    tabs: list[dict[str, object]] = []
    lines, n = gam._section_wiring(tabs)
    text = "\n".join(lines)
    assert n >= 4
    assert "### B5." in text
    unset_auto = any(
        r[1] == "unset" and r[4] == f"`{gam.mp.AUTO_MERGE}`"
        for t in tabs
        if t["header"][0] == "deployment"  # type: ignore[index]
        for r in t["rows"]  # type: ignore[attr-defined]
    )
    assert (gam.pe.PATH_RISK_FLOOR_ENV in text.split("### B5.")[1]) == unset_auto
