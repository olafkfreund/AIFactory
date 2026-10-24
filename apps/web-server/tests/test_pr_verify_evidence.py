"""The verify verdict and its vote split are recorded in the PR body (Factory#1963)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_WS = Path(__file__).resolve().parents[1]
for _p in (str(_WS), str(_WS.parent / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import server.services.pr_endgame as pe  # noqa: E402

REAL_WRITE = pe.write_verify_evidence
VOTE = {"splits": {"3-0": 4, "2-1": 1}, "calls": 3, "failed_calls": 0}


def _sig(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tfactory_verdict": "pass",
        "host_ci_green": True,
        "achieved_val": 3,
        "val_floor": 2,
        "ci_parity": True,
    }
    return base | over


def _block(
    vote: object = VOTE, *, head: str | None = "deadbeefcafe", **over: object
) -> str:
    return pe.verify_evidence_block(_sig(**over), vote, head=head, allowed=True)  # type: ignore[arg-type]


def test_block_renders_verdict_votes_and_floor() -> None:
    b = _block()
    lines = b.splitlines()
    assert lines[0] == pe._EVIDENCE_START and lines[-1] == pe._EVIDENCE_END
    assert pe._EVIDENCE_LINE in b
    assert "Verdict: pass (TFactory, model-assisted) at `deadbeefcafe`" in b
    assert "Votes: 2-1 x1, 3-0 x4 (3 calls, 0 failed)" in b
    assert "host CI green: yes; VAL: VAL-3 (floor VAL-2): met; CI parity: yes" in b
    assert "Merge gate: allowed" in b


@pytest.mark.parametrize(
    ("verdict", "word"),
    [(None, "hold"), ("handback", "fail"), ("fail", "fail"), ("pass", "pass")],
)
def test_verdict_mapping(verdict: object, word: str) -> None:
    assert f"Verdict: {word} " in _block(tfactory_verdict=verdict)


def test_votes_not_reported_and_hostile_input_dropped() -> None:
    assert "Votes: not reported" in _block({})
    assert "Votes: not reported" in _block(None)
    hostile = {"splits": {"3-0\n@x": 1, "2-1": True, "1-0": 2}, "calls": True}
    b = _block(hostile)
    assert (
        "Votes: 1-0 x2\n" in b and "@x" not in b and "calls" not in b.split("Votes")[1]
    )


def test_floor_not_met_and_red_ci_and_bad_head() -> None:
    b = _block(achieved_val="VAL-2", val_floor=3, host_ci_green=False, ci_parity=False)
    assert "host CI green: no; VAL: VAL-2 (floor VAL-3): not met; CI parity: no" in b
    assert "at `" not in _block(head="not-hex!")


class _Runner:
    def __init__(self, body: str = "", *, view_ok: bool = True, boom: bool = False):
        self.body, self.view_ok, self.boom = body, view_ok, boom
        self.edits: list[list[str]] = []

    def __call__(self, argv: list[str], _cwd: str | None) -> pe.CmdResult:
        if self.boom:
            raise RuntimeError("x")
        if argv[2] == "view":
            return pe.CmdResult(0 if self.view_ok else 1, self.body, "")
        self.edits.append(argv)
        return pe.CmdResult(0, "", "")


def test_write_appends_then_replaces_in_place() -> None:
    r = _Runner("intro")
    assert REAL_WRITE("o", "r", 7, "NEW1", runner=r)
    assert r.edits[0][-1] == "intro\n\nNEW1"
    old = f"before\n{pe._EVIDENCE_START}\nold\n{pe._EVIDENCE_END}\nafter"
    r = _Runner(old)
    assert REAL_WRITE("o", "r", 7, "NEW2", runner=r)
    assert r.edits[0][-1] == "before\nNEW2\nafter"


def test_write_never_writes_after_failed_read_or_raises() -> None:
    r = _Runner(view_ok=False)
    assert REAL_WRITE("o", "r", 7, "B", runner=r) is False and r.edits == []
    assert REAL_WRITE("o", "r", 7, "B", runner=_Runner(boom=True)) is False


async def _endgame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ci: Any
) -> tuple[dict[str, object], list[tuple[int, str]]]:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    monkeypatch.setattr("server.services.job_state_store.store_enabled", lambda: True)
    monkeypatch.setattr(
        "server.services.project_settings_store.is_loaded", lambda _p: True
    )
    spec = tmp_path / ".aifactory" / "specs" / "001-x"
    spec.mkdir(parents=True)
    writes: list[tuple[int, str]] = []

    def _v(*_a: object, vote: dict[str, Any] | None = None, **_k: object) -> str:
        assert vote is not None
        vote.update({"splits": {"3-0": 2}})
        return "pass"

    monkeypatch.setattr(pe, "merge_pr", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "create_pr", lambda **_k: 42)
    monkeypatch.setattr(pe, "_pr_title_body", lambda _d, _s: ("t", "b"))
    monkeypatch.setattr("pfactory.tfactory_client.tfactory_verdict", _v)
    monkeypatch.setattr(pe, "host_ci_green", host_ci)
    monkeypatch.setattr(pe, "required_checks_green", lambda *_a, **_k: True)
    monkeypatch.setattr(pe, "_pr_head", lambda *_a: "deadbeef")
    monkeypatch.setattr(
        pe,
        "write_verify_evidence",
        lambda _o, _r, pr, block, **_k: writes.append((pr, block)) or True,
    )
    real = pe.watch_and_finish

    async def _watch(**kw: Any) -> dict[str, object]:
        out: dict[str, object] = await real(**kw, poll_interval=0, max_minutes=1)
        return out

    monkeypatch.setattr(pe, "watch_and_finish", _watch)
    result = await pe.run_pr_endgame(
        spec_dir=spec,
        spec_id="001-x",
        worktree=spec,
        branch="aifactory/001-x",
        base="main",
        repo="owner/repo",
        auto_merge=True,
        review_tier="auto",
        reviewer="aifactory",
        review_fn=lambda: pe.ReviewState("approved"),
        background=False,
        trusted=None,
    )
    return result, writes


async def test_gate_records_verdict_once_when_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, writes = await _endgame(tmp_path, monkeypatch, lambda *_a, **_k: True)
    assert result["merged"] is True
    assert len(writes) == 1 and writes[0][0] == 42
    assert "Verdict: pass" in writes[0][1] and "3-0 x2" in writes[0][1]


async def test_gate_rewrites_when_the_floor_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq = iter([False, True])
    state = {"last": False}

    def _ci(*_a: object, **_k: object) -> bool:
        state["last"] = next(seq, True)
        return state["last"]

    result, writes = await _endgame(tmp_path, monkeypatch, _ci)
    assert result["merged"] is True
    assert len(writes) == 2
    assert "Merge gate: held" in writes[0][1] and "Merge gate: allowed" in writes[1][1]
