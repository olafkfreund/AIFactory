"""A managed workspace mirror must re-sync even when its tree is dirty.

``clone_or_update`` keeps a disposable mirror of origin. A consumer (e.g. the
benchmark harness) can leave local edits + untracked files in that clone. The
old code ran ``git pull --ff-only``, which aborts on a dirty tree and surfaced
as ``HTTP 400 "Clone failed"`` from project registration. The sync now falls
back to ``git reset --hard`` + ``git clean -fd`` so registration succeeds
against origin's tip instead of failing.

``_run_git`` restricts transports to https/ssh/git (#323 C5), so we can't drive
a real ``file://`` remote here — we monkeypatch ``_run_git`` and assert the exact
command sequence the fallback issues.
"""

import pytest
from server.services import project_workspace_service as pws


@pytest.mark.asyncio
async def test_dirty_existing_clone_falls_back_to_reset_and_clean(
    tmp_path, monkeypatch
):
    # Make the existing-clone branch fire: a ``.git`` dir under the workspace.
    root = tmp_path / "ws-root"
    (root / "ws" / ".git").mkdir(parents=True)

    calls: list[list[str]] = []

    async def fake_run_git(args, *, cwd, timeout):
        calls.append(list(args))
        # Simulate a dirty tree: the fast-forward pull aborts, everything
        # else succeeds.
        if args[:2] == ["pull", "--ff-only"]:
            raise pws.GitOperationError("local changes would be overwritten")
        return ""

    monkeypatch.setattr(pws, "_run_git", fake_run_git)

    # Must NOT raise (old behaviour raised GitOperationError -> 400).
    ws = await pws.clone_or_update(
        git_url="https://example.test/repo.git",
        branch="main",
        slug="ws",
        root=root,
    )

    assert ws == root / "ws"
    assert calls == [
        ["fetch", "--prune", "origin"],
        ["checkout", "main"],
        ["pull", "--ff-only"],
        ["reset", "--hard", "origin/main"],
        ["clean", "-fd"],
    ]


@pytest.mark.asyncio
async def test_no_branch_resets_to_fetch_head(tmp_path, monkeypatch):
    root = tmp_path / "ws-root"
    (root / "ws" / ".git").mkdir(parents=True)

    calls: list[list[str]] = []

    async def fake_run_git(args, *, cwd, timeout):
        calls.append(list(args))
        if args[:2] == ["pull", "--ff-only"]:
            raise pws.GitOperationError("dirty")
        return ""

    monkeypatch.setattr(pws, "_run_git", fake_run_git)

    await pws.clone_or_update(
        git_url="https://example.test/repo.git", slug="ws", root=root
    )

    # No branch -> no checkout, and the reset targets the fetched remote tip.
    assert ["checkout", "main"] not in calls
    assert ["reset", "--hard", "FETCH_HEAD"] in calls
    assert ["clean", "-fd"] in calls
