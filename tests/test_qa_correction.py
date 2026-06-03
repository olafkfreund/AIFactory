"""Tests for the inbound correction receiver (TFactory→AIFactory, #317).

Covers ``qa.correction.apply_correction`` — the seam the REST route calls. The
QA Fixer is injected so the suite needs no SDK: preview writes/starts nothing;
confirm writes ``QA_FIX_REQUEST.md`` and invokes the (fake) fixer.
"""

from __future__ import annotations

from pathlib import Path

from qa.correction import apply_correction, write_fix_request

FIX_MD = "# QA Fix Request\n\nLogin returns 500 instead of 200. Fix the handler."


async def test_preview_writes_nothing_and_starts_nothing(tmp_path: Path) -> None:
    calls = []

    async def fake_fixer(spec_dir):
        calls.append(spec_dir)
        return {"status": "qa_fixing"}

    res = await apply_correction(tmp_path, FIX_MD, confirm=False, fixer_fn=fake_fixer)

    assert res["success"] is True
    assert res["confirm"] is False
    assert res["started"] is False
    assert res["would_write"].endswith("QA_FIX_REQUEST.md")
    assert calls == []  # fixer never invoked on preview
    assert not (tmp_path / "QA_FIX_REQUEST.md").exists()  # nothing written


async def test_confirm_writes_fix_request_and_runs_fixer(tmp_path: Path) -> None:
    seen = {}

    async def fake_fixer(spec_dir):
        seen["spec_dir"] = spec_dir
        seen["md"] = (Path(spec_dir) / "QA_FIX_REQUEST.md").read_text()
        return {"status": "qa_fixing", "scheduled": True}

    res = await apply_correction(tmp_path, FIX_MD, confirm=True, fixer_fn=fake_fixer)

    assert res["success"] is True
    assert res["confirm"] is True
    assert res["started"] is True
    assert res["wrote"].endswith("QA_FIX_REQUEST.md")
    assert res["status"] == "qa_fixing"
    # The fixer saw the file already written with our content.
    assert seen["spec_dir"] == tmp_path
    assert seen["md"] == FIX_MD
    assert (tmp_path / "QA_FIX_REQUEST.md").read_text() == FIX_MD


async def test_confirm_overwrites_existing_fix_request(tmp_path: Path) -> None:
    (tmp_path / "QA_FIX_REQUEST.md").write_text("stale content")

    async def fake_fixer(spec_dir):
        return {"status": "qa_fixing"}

    await apply_correction(tmp_path, FIX_MD, confirm=True, fixer_fn=fake_fixer)
    assert (tmp_path / "QA_FIX_REQUEST.md").read_text() == FIX_MD


def test_write_fix_request_returns_path(tmp_path: Path) -> None:
    p = write_fix_request(tmp_path, FIX_MD)
    assert p == tmp_path / "QA_FIX_REQUEST.md"
    assert p.read_text() == FIX_MD
