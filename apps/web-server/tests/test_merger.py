"""Tests for the merger (services.merger) -- see that module's docstring.

The merger opens PRs for stranded task branches (real commits, pushed, no open
PR) without requiring QA approval. Every git/gh call goes through an injectable
runner (reused from ``pr_endgame``), so these tests touch no network/git.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

import pytest  # noqa: E402
from server.services import merger as mg  # noqa: E402
from server.services.pr_endgame import CmdResult  # noqa: E402
from server.services.task_control import read_control, write_control  # noqa: E402


class FakeRunner:
    """Routes argv -> CmdResult by matching substrings; records calls."""

    def __init__(self, routes: dict[str, CmdResult]):
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, argv, _cwd=None):
        self.calls.append(argv)
        joined = " ".join(argv)
        # Longest (most specific) match wins, so a narrow override (e.g. one
        # spec's branch) is not shadowed by a broader catch-all registered
        # earlier in the same routes dict.
        matches = [
            (needle, result)
            for needle, result in self.routes.items()
            if needle in joined
        ]
        if matches:
            return max(matches, key=lambda pair: len(pair[0]))[1]
        return CmdResult(0, "", "")

    def saw(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)


def _spec(
    tmp_path: Path,
    spec_id: str,
    *,
    repo="o/r",
    base_branch=None,
    tenant=None,
) -> Path:
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    wt.mkdir(parents=True)
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    req = {"github_repo": repo, "title": f"Task {spec_id}"}
    (spec_dir / "requirements.json").write_text(json.dumps(req))
    meta: dict[str, object] = {}
    if base_branch:
        meta["base_branch"] = base_branch
    if tenant:
        meta["tenant_id"] = tenant
    if meta:
        (spec_dir / "task_metadata.json").write_text(json.dumps(meta))
    return spec_dir


# ── _find_pr ────────────────────────────────────────────────────────────────


def _prs(*items: tuple[int, str, str]) -> CmdResult:
    return CmdResult(
        0,
        json.dumps(
            [{"number": n, "state": st, "createdAt": at} for n, st, at in items]
        ),
        "",
    )


def test_find_pr_none():
    r = FakeRunner({"pr list": CmdResult(0, "[]", "")})
    assert mg._find_pr("o", "r", "b", r) == (True, None, None)


def test_find_pr_found_open():
    r = FakeRunner({"pr list": _prs((42, "OPEN", "2026-09-01"))})
    assert mg._find_pr("o", "r", "b", r) == (True, 42, "OPEN")


def test_find_pr_queries_every_state():
    """#2586: an open-only query cannot see a merged PR, and a merged branch
    measured locally looks like fresh work."""
    r = FakeRunner({"pr list": CmdResult(0, "[]", "")})
    mg._find_pr("o", "r", "b", r)
    assert r.saw("--state all")


def test_find_pr_prefers_open_over_newer_merged():
    r = FakeRunner(
        {"pr list": _prs((7, "MERGED", "2026-09-10"), (9, "OPEN", "2026-09-01"))}
    )
    assert mg._find_pr("o", "r", "b", r) == (True, 9, "OPEN")


def test_find_pr_most_recent_when_none_open():
    r = FakeRunner(
        {"pr list": _prs((3, "CLOSED", "2026-09-01"), (8, "MERGED", "2026-09-08"))}
    )
    assert mg._find_pr("o", "r", "b", r) == (True, 8, "MERGED")


def test_find_pr_query_failure_is_unmeasured_not_none():
    """Finding #1: a failed `gh pr list` must be distinguishable from a
    successful query that found nothing -- else the sweep proceeds to
    `gh pr create` on an idempotency check that was never actually made."""
    r = FakeRunner({"pr list": CmdResult(1, "", "rate limited")})
    assert mg._find_pr("o", "r", "b", r) == (False, None, None)


def test_find_pr_garbled_output_is_unmeasured_not_none():
    r = FakeRunner({"pr list": CmdResult(0, "<html>502</html>", "")})
    assert mg._find_pr("o", "r", "b", r) == (False, None, None)


# ── _branch_ahead_and_changed ────────────────────────────────────────────────


def test_branch_ahead_and_changed_measures(tmp_path):
    r = FakeRunner(
        {
            "fetch origin main": CmdResult(0, "", ""),
            "fetch origin aifactory/1": CmdResult(0, "", ""),
            "rev-list --count": CmdResult(0, "3\n", ""),
            "diff --name-only": CmdResult(0, "a.py\nb.py\n", ""),
        }
    )
    got = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert got == (3, 2, 3)


def test_branch_ahead_and_changed_unmeasurable_when_branch_nowhere(tmp_path):
    r = FakeRunner(
        {
            "fetch origin main": CmdResult(0, "", ""),
            "fetch origin aifactory/1": CmdResult(1, "", "couldn't find remote ref"),
            "rev-parse --verify": CmdResult(1, "", ""),
        }
    )
    got = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert got == (None, None, None)


def test_branch_ahead_and_changed_zero_is_measured_not_unmeasurable(tmp_path):
    """Requirement #5: a real ahead_by of 0 is a MEASUREMENT, distinct from
    the fetch failing outright (which is None, "don't know")."""
    r = FakeRunner(
        {
            "fetch origin": CmdResult(0, "", ""),
            "rev-list --count": CmdResult(0, "0\n", ""),
            "diff --name-only": CmdResult(0, "", ""),
        }
    )
    got = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert got == (0, 0, 0)


def test_branch_ahead_and_changed_unmeasurable_when_base_fetch_fails(tmp_path):
    """Finding #2: the base fetch's result was previously discarded -- a stale
    `origin/main` still produced a plausible-looking (wrong) comparison."""
    r = FakeRunner(
        {
            "fetch origin main": CmdResult(1, "", "connection reset"),
            "fetch origin aifactory/1": CmdResult(0, "", ""),
            "rev-list --count": CmdResult(0, "3\n", ""),
            "diff --name-only": CmdResult(0, "a.py\n", ""),
        }
    )
    got = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert got == (None, None, None)


def test_branch_ahead_and_changed_unmeasurable_when_diff_fails(tmp_path):
    """Finding #3: a failed `git diff` must not surface as changed_files=None
    while ahead_by is still a real number -- `not changed_files` would then
    read a real branch as empty. A failed diff makes the WHOLE pair
    unmeasurable."""
    r = FakeRunner(
        {
            "fetch origin main": CmdResult(0, "", ""),
            "fetch origin aifactory/1": CmdResult(0, "", ""),
            "rev-list --count": CmdResult(0, "3\n", ""),
            "diff --name-only": CmdResult(1, "", "ambiguous argument"),
        }
    )
    got = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert got == (None, None, None)


# ── honest_pr_title_and_body ─────────────────────────────────────────────────


def test_honest_body_states_no_gate_evidence_and_qa_not_run(tmp_path):
    spec_dir = _spec(tmp_path, "001-x")
    title, body = mg.honest_pr_title_and_body(spec_dir, "001-x", tmp_path, None)
    assert title == "Task 001-x"
    assert "review surface, not a certificate" in body
    assert "no verification gates recorded" in body
    assert "QA sign-off: not run" in body


def test_honest_body_reports_qa_signoff_status(tmp_path):
    spec_dir = _spec(tmp_path, "002-x")
    (spec_dir / "implementation_plan.json").write_text(
        json.dumps({"qa_signoff": {"status": "rejected"}})
    )
    _title, body = mg.honest_pr_title_and_body(spec_dir, "002-x", tmp_path, "medium")
    assert "QA sign-off: rejected" in body
    assert "Review tier: medium" in body


def test_honest_body_never_claims_qa_passed(tmp_path):
    """The one claim this body must NEVER make -- pr_endgame's body does, and
    that's exactly the distinction a reviewer must be able to draw."""
    spec_dir = _spec(tmp_path, "003-x")
    _title, body = mg.honest_pr_title_and_body(spec_dir, "003-x", tmp_path, None)
    assert "clean" not in body.lower()
    assert "qa-passed" not in body.lower()


def test_honest_body_links_origin_issue(tmp_path):
    spec_dir = _spec(tmp_path, "004-x")
    req = json.loads((spec_dir / "requirements.json").read_text())
    req["provenance"] = {"issue_number": 77}
    (spec_dir / "requirements.json").write_text(json.dumps(req))
    _title, body = mg.honest_pr_title_and_body(spec_dir, "004-x", tmp_path, None)
    assert "Fixes #77" in body


# ── _process_spec / sweep ────────────────────────────────────────────────────


def _routes(**overrides) -> dict[str, CmdResult]:
    base = {
        "rev-parse --abbrev-ref HEAD": CmdResult(0, "aifactory/001-x", ""),
        "pr list": CmdResult(0, "", ""),
        "fetch origin": CmdResult(0, "", ""),
        "rev-list --count": CmdResult(0, "3\n", ""),
        "diff --name-only": CmdResult(0, "a.py\n", ""),
        "auth setup-git": CmdResult(0, "", ""),
        "git push": CmdResult(0, "", ""),
        "pr create": CmdResult(0, "https://github.com/o/r/pull/5", ""),
    }
    base.update(overrides)
    return base


def test_process_spec_opens_pr_for_stranded_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes())
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out == {
        "task": "proj:001-x",
        "action": "opened",
        "pr": 5,
        "reason": None,
        "unpushed": 3,
    }
    assert r.saw("pr create")


def test_process_spec_idempotent_when_pr_already_open(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes(**{"pr list": _prs((9, "OPEN", "2026-09-01"))}))
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out == {
        "task": "proj:001-x",
        "action": "already_open",
        "pr": 9,
        "reason": None,
    }
    assert not r.saw("pr create"), "an already-open PR must never be re-opened"


def test_process_spec_merged_pr_opens_nothing_even_when_branch_ahead(
    tmp_path, monkeypatch
):
    """#2586: task 001 was squash-merged; its local branch still carries the
    commits under other SHAs, so it measures as 'ahead'. A merged PR is a
    decision already made -- never open a second one."""
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes(**{"pr list": _prs((35, "MERGED", "2026-09-08"))}))
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out == {"task": "proj:001-x", "action": "merged", "pr": 35, "reason": None}
    assert not r.saw("pr create")
    assert not r.saw("git push")


def test_process_spec_closed_pr_opens_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "003-x")
    r = FakeRunner(_routes(**{"pr list": _prs((38, "CLOSED", "2026-09-08"))}))
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out["action"] == "closed"
    assert out["pr"] == 38
    assert not r.saw("pr create")


def test_process_spec_skips_empty_branch_no_pr_opened(tmp_path, monkeypatch):
    """Requirement #5: ahead_by == 0 gets no PR."""
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes(**{"rev-list --count": CmdResult(0, "0\n", "")}))
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out["action"] == "skipped"
    assert "no_content" in out["reason"]
    assert not r.saw("pr create")


def test_process_spec_skips_zero_changed_files_even_if_ahead(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes(**{"diff --name-only": CmdResult(0, "", "")}))
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out["action"] == "skipped"
    assert "no_content" in out["reason"]
    assert not r.saw("pr create")


def test_process_spec_never_drops_silently_when_unmeasurable(tmp_path, monkeypatch):
    """Requirement #2: a branch that cannot be measured still gets a reason."""
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(
        _routes(
            **{
                "fetch origin aifactory/001-x": CmdResult(1, "", "no such ref"),
                "rev-parse --verify": CmdResult(1, "", ""),
            }
        )
    )
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out["action"] == "skipped"
    assert out["reason"] is not None
    assert not r.saw("pr create")


def test_process_spec_skips_not_duplicates_when_pr_list_query_fails(
    tmp_path, monkeypatch
):
    """Finding #1 end-to-end: a failed idempotency check must never fall
    through to `gh pr create` -- that would open a duplicate PR precisely
    when the duplicate-check couldn't run."""
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes(**{"pr list": CmdResult(1, "", "rate limited")}))
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out["action"] == "skipped"
    assert "unmeasurable" in out["reason"]
    assert not r.saw("pr create")


def test_process_spec_never_merges_regardless_of_auto_merge_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    monkeypatch.setenv("AIFACTORY_AUTO_MERGE", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes())
    mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert not r.saw("pr merge"), "the merger must never merge -- that stays human"


def test_process_spec_respects_auto_pr_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "false")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes())
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out["action"] == "skipped"
    assert out["reason"] == "auto_pr_disabled"
    assert not r.saw("pr create")


def test_process_spec_dry_run_opens_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes())
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=True, runner=r)
    assert out["action"] == "would_open"
    assert not r.saw("pr create")


def test_process_spec_no_worktree_is_skipped_not_silent(tmp_path):
    spec_dir = tmp_path / ".aifactory" / "specs" / "001-x"
    spec_dir.mkdir(parents=True)
    r = FakeRunner({})
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out["action"] == "skipped"
    assert out["reason"] == "no_worktree_or_resolvable_repo"


# ── sweep (project enumeration) ──────────────────────────────────────────────


def test_sweep_accounts_for_every_spec(tmp_path, monkeypatch):
    """Requirement #2: every branch examined shows up in the report."""
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    proj = tmp_path / "proj"
    _spec(proj, "001-opens")
    _spec(proj, "002-empty")

    monkeypatch.setattr(mg, "load_projects", lambda: {"p1": {"path": str(proj)}})
    monkeypatch.setattr(mg, "resolve_project_path", lambda _pid: proj)

    # One runner shared across both specs; each spec's worktree path (the
    # `cwd` every call gets) embeds its own spec id, so route on that rather
    # than on argv content shared between the two (both start on "HEAD").
    def runner(argv, cwd=None):
        joined = " ".join(argv)
        spec_id = "002-empty" if cwd and "002-empty" in cwd else "001-opens"
        if joined.startswith("git rev-parse"):
            return CmdResult(0, f"aifactory/{spec_id}", "")
        if "rev-list --count" in joined:
            return CmdResult(0, "0\n" if spec_id == "002-empty" else "3\n", "")
        if "diff --name-only" in joined:
            return CmdResult(0, "" if spec_id == "002-empty" else "a.py\n", "")
        if "pr list" in joined:
            return CmdResult(0, "", "")
        if "pr create" in joined:
            return CmdResult(0, "https://github.com/o/r/pull/5", "")
        return CmdResult(0, "", "")

    report = mg.sweep(dry_run=False, runner=runner)
    tasks = {r["task"] for r in report["results"]}
    assert tasks == {"p1:001-opens", "p1:002-empty"}
    actions = {r["task"]: r["action"] for r in report["results"]}
    assert actions["p1:001-opens"] == "opened"
    assert actions["p1:002-empty"] == "skipped"
    assert report["counts"]["opened"] == 1
    assert report["counts"]["skipped"] == 1


def test_sweep_one_broken_spec_does_not_hide_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    proj = tmp_path / "proj"
    _spec(proj, "001-ok")
    _spec(proj, "002-ok")

    monkeypatch.setattr(mg, "load_projects", lambda: {"p1": {"path": str(proj)}})
    monkeypatch.setattr(mg, "resolve_project_path", lambda _pid: proj)

    calls = {"n": 0}

    def flaky_process(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"task": "p1:002-ok", "action": "opened", "pr": 1, "reason": None}

    monkeypatch.setattr(mg, "_process_spec", flaky_process)
    report = mg.sweep(dry_run=False, runner=FakeRunner({}))
    assert len(report["results"]) == 2
    assert any(r["reason"] == "sweep_error (see logs)" for r in report["results"])
    assert any(r["action"] == "opened" for r in report["results"])


def test_sweep_project_ids_restricts_scope(tmp_path, monkeypatch):
    """The route passes the caller's visible projects; sweep must honour it
    rather than always scanning every registered project (finding #4)."""
    monkeypatch.setattr(
        mg,
        "load_projects",
        lambda: {"p1": {"path": "/should/not/be/read"}, "p2": {"path": "/nope"}},
    )
    seen: list[str] = []

    def fake_process(project_id, *_a, **_k):
        seen.append(project_id)
        return {
            "task": f"{project_id}:x",
            "action": "skipped",
            "pr": None,
            "reason": "r",
        }

    monkeypatch.setattr(mg, "_process_spec", fake_process)
    monkeypatch.setattr(mg, "resolve_project_path", lambda _pid: tmp_path)
    monkeypatch.setattr(mg, "get_spec_dirs", lambda _p: [tmp_path / "spec-1"])
    mg.sweep(dry_run=True, runner=FakeRunner({}), project_ids=["p1"])
    assert seen == ["p1"], "sweep scanned a project outside the restricted scope"


def test_sweep_tenant_filters_specs_not_projects(tmp_path, monkeypatch):
    """Finding #1 (#1554): org membership alone does not separate two
    tenants sharing one org -- filtering must happen at the SPEC level
    (mirrors routes/tasks.py's list_tasks spec_tenant filter), not merely at
    project/org scope."""
    proj = tmp_path / "proj"
    _spec(proj, "001-acme", tenant="acme")
    _spec(proj, "002-other", tenant="other")
    _spec(proj, "003-unstamped")  # => "default" tenant, must not match "acme"

    monkeypatch.setattr(mg, "load_projects", lambda: {"p1": {"path": str(proj)}})
    monkeypatch.setattr(mg, "resolve_project_path", lambda _pid: proj)

    seen: list[str] = []

    def fake_process(project_id, _project_path, spec_dir, **_kwargs):
        seen.append(spec_dir.name)
        return {
            "task": f"{project_id}:{spec_dir.name}",
            "action": "skipped",
            "pr": None,
            "reason": "r",
        }

    monkeypatch.setattr(mg, "_process_spec", fake_process)
    mg.sweep(dry_run=True, runner=FakeRunner({}), tenant="acme")
    assert seen == ["001-acme"], "a non-matching or unstamped spec leaked through"


def test_sweep_tenant_none_scans_every_tenant(tmp_path, monkeypatch):
    """tenant=None (multi-tenant mode off) must be a no-op filter -- the
    existing single-tenant behaviour byte-identical."""
    proj = tmp_path / "proj"
    _spec(proj, "001-acme", tenant="acme")
    _spec(proj, "002-other", tenant="other")

    monkeypatch.setattr(mg, "load_projects", lambda: {"p1": {"path": str(proj)}})
    monkeypatch.setattr(mg, "resolve_project_path", lambda _pid: proj)

    seen: list[str] = []

    def fake_process(project_id, _project_path, spec_dir, **_kwargs):
        seen.append(spec_dir.name)
        return {
            "task": f"{project_id}:{spec_dir.name}",
            "action": "skipped",
            "pr": None,
            "reason": "r",
        }

    monkeypatch.setattr(mg, "_process_spec", fake_process)
    mg.sweep(dry_run=True, runner=FakeRunner({}))
    assert sorted(seen) == ["001-acme", "002-other"]


def test_sweep_spec_enumeration_error_does_not_hide_later_projects(
    tmp_path, monkeypatch
):
    """Finding #4 (#1554): get_spec_dirs used to sit OUTSIDE the per-project
    try, so a permission error enumerating one project's specs aborted the
    whole sweep -- every project after it silently never got scanned. The
    merger's entire purpose is "never drop work"; this is that rule broken
    at the project level."""
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    proj_bad = tmp_path / "bad"
    proj_good = tmp_path / "good"
    proj_bad.mkdir()
    _spec(proj_good, "001-ok")

    monkeypatch.setattr(
        mg,
        "load_projects",
        lambda: {"pbad": {"path": str(proj_bad)}, "pgood": {"path": str(proj_good)}},
    )
    monkeypatch.setattr(
        mg,
        "resolve_project_path",
        lambda pid: proj_bad if pid == "pbad" else proj_good,
    )
    real_get_spec_dirs = mg.get_spec_dirs

    def flaky_get_spec_dirs(project_path):
        if project_path == proj_bad:
            raise PermissionError("denied")
        return real_get_spec_dirs(project_path)

    monkeypatch.setattr(mg, "get_spec_dirs", flaky_get_spec_dirs)

    seen: list[str] = []

    def fake_process(project_id, _project_path, spec_dir, **_kwargs):
        seen.append(f"{project_id}:{spec_dir.name}")
        return {
            "task": f"{project_id}:{spec_dir.name}",
            "action": "opened",
            "pr": 1,
            "reason": None,
        }

    monkeypatch.setattr(mg, "_process_spec", fake_process)
    report = mg.sweep(dry_run=False, runner=FakeRunner({}))
    assert seen == ["pgood:001-ok"], (
        "the good project (after the bad one) must still be scanned -- "
        "silently dropping it is the exact bug this fixes"
    )
    assert any(
        r["reason"] == "spec_enumeration_error (see logs)" for r in report["results"]
    )


def test_sweep_unreadable_tenant_stamp_never_processed_by_default_sweep(
    tmp_path, monkeypatch
):
    """Finding #1 generalised (#1555): a spec with an unreadable stamp must
    NEVER be treated as belonging to the "default" tenant -- that is exactly
    what would let a default-tenant sweep push/PR a spec whose real tenant is
    unknown. It must be excluded from processing AND recorded, not silently
    dropped from either the scan or the report."""
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    proj = tmp_path / "proj"
    _spec(proj, "001-good", tenant="default")
    bad_id = "002-corrupt"
    (proj / ".aifactory" / "worktrees" / "tasks" / bad_id).mkdir(parents=True)
    bad_spec = proj / ".aifactory" / "specs" / bad_id
    bad_spec.mkdir(parents=True)
    (bad_spec / "requirements.json").write_text(json.dumps({"github_repo": "o/r"}))
    (bad_spec / "task_metadata.json").write_text("{not valid json")

    monkeypatch.setattr(mg, "load_projects", lambda: {"p1": {"path": str(proj)}})
    monkeypatch.setattr(mg, "resolve_project_path", lambda _pid: proj)

    seen: list[str] = []

    def fake_process(project_id, _project_path, spec_dir, **_kwargs):
        seen.append(spec_dir.name)
        return {
            "task": f"{project_id}:{spec_dir.name}",
            "action": "opened",
            "pr": 1,
            "reason": None,
        }

    monkeypatch.setattr(mg, "_process_spec", fake_process)
    report = mg.sweep(dry_run=False, runner=FakeRunner({}), tenant="default")
    assert seen == ["001-good"], "an unreadable-stamp spec was swept as default tenant"
    assert any(
        r["task"] == f"p1:{bad_id}"
        and r["reason"] == "tenant_stamp_unreadable (see logs)"
        for r in report["results"]
    ), "the unreadable spec must be recorded, not silently dropped"


# ── measuring against real git (#2586) ──────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    # Fixed argv against a test-owned tmp repo; check=True so a failed git
    # setup step fails the test instead of passing on an empty repo.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)


@pytest.fixture
def repos(tmp_path):
    """A bare origin with `main`, plus a clone on `aifactory/1` pushed at base."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    wt = tmp_path / "wt"
    _git(tmp_path, "clone", "-q", str(origin), str(wt))
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    _git(wt, "checkout", "-qb", "main")
    _commit(wt, "base.txt")
    _git(wt, "push", "-q", "origin", "main")
    _git(wt, "checkout", "-qb", "aifactory/1")
    _git(wt, "push", "-q", "origin", "aifactory/1")
    return wt


def _measure(wt: Path):
    return mg._branch_ahead_and_changed(
        wt, "main", "aifactory/1", mg.pe._default_runner
    )


def test_real_git_unpushed_local_commits_are_work_not_no_content(repos):
    """#2586's five stranded tasks: commits only in the local branch, origin
    still at base. The origin-only measurement said ahead_by=0."""
    for n in ("a.py", "b.py", "c.py"):
        _commit(repos, n)
    assert _measure(repos) == (3, 3, 3)


def test_real_git_pushed_branch_has_nothing_unpushed(repos):
    _commit(repos, "a.py")
    _git(repos, "push", "-q", "origin", "aifactory/1")
    assert _measure(repos) == (1, 1, 0)


def test_real_git_stale_local_ref_uses_origin(repos, tmp_path):
    """Packed path: the Job pushed to origin, this worktree's ref is stale.
    Measuring only the local ref would report the Job's work as empty."""
    other = tmp_path / "job"
    _git(
        tmp_path,
        "clone",
        "-q",
        "-b",
        "aifactory/1",
        str(tmp_path / "origin.git"),
        str(other),
    )
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    _commit(other, "job.py")
    _git(other, "push", "-q", "origin", "aifactory/1")
    assert _measure(repos) == (1, 1, 0)


def test_real_git_diverged_is_a_skip_never_a_force(repos, tmp_path):
    other = tmp_path / "job"
    _git(
        tmp_path,
        "clone",
        "-q",
        "-b",
        "aifactory/1",
        str(tmp_path / "origin.git"),
        str(other),
    )
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    _commit(other, "theirs.py")
    _git(other, "push", "-q", "origin", "aifactory/1")
    _commit(repos, "ours.py")
    with pytest.raises(mg._SkipTask, match="diverged"):
        _measure(repos)


def test_real_git_base_advancing_is_not_counted_as_branch_work(repos):
    """Three-dot diff: files that reached main after the branch point are not
    this task's changes."""
    _commit(repos, "task.py")
    _git(repos, "checkout", "-q", "main")
    _commit(repos, "later-on-main.py")
    _git(repos, "push", "-q", "origin", "main")
    _git(repos, "checkout", "-q", "aifactory/1")
    ahead, changed, _unpushed = _measure(repos)
    assert (ahead, changed) == (1, 1)


# ── status follows the PR (#2586) ───────────────────────────────────────────


def _in_review(spec_dir: Path, reason: str = "errors") -> Path:
    write_control(spec_dir, status="human_review", review_reason=reason)
    return spec_dir


@pytest.mark.parametrize(
    ("result", "status", "reason"),
    [
        ({"action": "opened", "pr": 5}, "human_review", "awaiting_merge"),
        ({"action": "already_open", "pr": 5}, "human_review", "awaiting_merge"),
        ({"action": "merged", "pr": 5}, "done", None),
        ({"action": "closed", "pr": 5}, "human_review", "pr_closed"),
        (
            {"action": "skipped", "reason": "no_content (ahead_by=0, changed_files=0)"},
            "human_review",
            "no_work",
        ),
    ],
)
def test_sync_status_maps_each_pr_outcome(tmp_path, result, status, reason):
    spec_dir = _in_review(_spec(tmp_path, "001-x"))
    assert mg._sync_status(spec_dir, result) is True
    control = read_control(spec_dir)
    assert control["status"] == status
    assert control.get("reviewReason") == reason
    assert control["updatedBy"] == "merger"


@pytest.mark.parametrize(
    "reason",
    [
        "ahead_by_unmeasurable (branch not found locally or on origin)",
        "open_pr_check_unmeasurable (gh pr list failed)",
        "diverged (local and origin branch both have unique commits)",
        "auto_pr_disabled",
    ],
)
def test_sync_status_undecided_skips_leave_status_alone(tmp_path, reason):
    """An unmeasured outcome says nothing about the task -- #2586's task 019
    (no branch anywhere) must not be relabelled as `no_work`."""
    spec_dir = _in_review(_spec(tmp_path, "019-x"))
    before = read_control(spec_dir)
    assert mg._sync_status(spec_dir, {"action": "skipped", "reason": reason}) is False
    assert read_control(spec_dir) == before


@pytest.mark.parametrize("status", ["in_progress", "backlog", "done"])
def test_sync_status_never_overrules_a_status_outside_human_review(tmp_path, status):
    spec_dir = _spec(tmp_path, "001-x")
    write_control(spec_dir, status=status)
    assert mg._sync_status(spec_dir, {"action": "opened", "pr": 5}) is False
    assert read_control(spec_dir)["status"] == status


def test_sync_status_unchanged_target_is_not_rewritten(tmp_path):
    spec_dir = _in_review(_spec(tmp_path, "001-x"), "awaiting_merge")
    assert mg._sync_status(spec_dir, {"action": "already_open", "pr": 5}) is False


def test_sync_status_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    spec_dir = _in_review(_spec(tmp_path, "001-x"))

    def boom(*_a, **_k):
        raise OSError("read-only fs")

    monkeypatch.setattr(mg, "write_control", boom)
    assert mg._sync_status(spec_dir, {"action": "opened", "pr": 5}) is False


def test_process_one_opens_and_syncs(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _in_review(_spec(tmp_path, "001-x"))
    out = mg.process_one("proj", tmp_path, spec_dir, runner=FakeRunner(_routes()))
    assert out["action"] == "opened"
    assert out["status_written"] is True
    assert read_control(spec_dir)["reviewReason"] == "awaiting_merge"


def _sweep_env(tmp_path, monkeypatch, pr_list: CmdResult):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    proj = tmp_path / "proj"
    spec_dir = _in_review(_spec(proj, "001-x"))
    monkeypatch.setattr(mg, "load_projects", lambda: {"p1": {"path": str(proj)}})
    monkeypatch.setattr(mg, "resolve_project_path", lambda _pid: proj)
    return spec_dir, FakeRunner(_routes(**{"pr list": pr_list}))


def test_sweep_writes_status_and_counts_it(tmp_path, monkeypatch):
    spec_dir, r = _sweep_env(tmp_path, monkeypatch, _prs((35, "MERGED", "2026-09-08")))
    report = mg.sweep(dry_run=False, runner=r)
    assert report["counts"]["merged"] == 1
    assert report["counts"]["status_written"] == 1
    assert read_control(spec_dir)["status"] == "done"


def test_sweep_dry_run_never_writes_status(tmp_path, monkeypatch):
    spec_dir, r = _sweep_env(tmp_path, monkeypatch, _prs((35, "MERGED", "2026-09-08")))
    control_file = spec_dir / "task_control.json"
    before = (control_file.read_bytes(), control_file.stat().st_mtime_ns)
    report = mg.sweep(dry_run=True, runner=r)
    assert report["counts"]["merged"] == 1
    assert report["counts"]["status_written"] == 0
    assert (control_file.read_bytes(), control_file.stat().st_mtime_ns) == before


# ── backstop loop (#2586) ───────────────────────────────────────────────────


def test_loop_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("AIFACTORY_MERGER_SWEEP", raising=False)
    assert mg.merger_sweep_enabled() is False
    monkeypatch.setenv("AIFACTORY_MERGER_SWEEP", "true")
    assert mg.merger_sweep_enabled() is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, True), ("true", True), ("flase", True), ("", True), ("false", False)],
)
def test_dry_run_fails_closed(monkeypatch, raw, expected):
    """Only an explicit "false" writes; a typo reports rather than opening PRs."""
    if raw is None:
        monkeypatch.delenv("AIFACTORY_MERGER_SWEEP_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("AIFACTORY_MERGER_SWEEP_DRY_RUN", raw)
    assert mg.dry_run() is expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("60", 60.0), ("0", 900.0), ("x", 900.0)]
)
def test_interval_rejects_nonsense(monkeypatch, raw, expected):
    monkeypatch.setenv("AIFACTORY_MERGER_SWEEP_INTERVAL_S", raw)
    assert mg.interval_s() == expected


def test_sweep_once_honours_dry_run_and_records_the_tick(monkeypatch):
    seen = []
    monkeypatch.setenv("AIFACTORY_MERGER_SWEEP_DRY_RUN", "false")
    monkeypatch.setattr(mg, "_last_tick_at", None)

    def fake_sweep(*, dry_run):
        seen.append(dry_run)
        return {"dry_run": dry_run, "results": [], "counts": {}}

    monkeypatch.setattr(mg, "sweep", fake_sweep)
    mg.sweep_once()
    assert seen == [False]
    assert mg.last_tick_at() is not None


def test_loop_survives_a_failing_tick_and_stops_cleanly(monkeypatch):
    monkeypatch.setenv("AIFACTORY_MERGER_SWEEP_INTERVAL_S", "0.01")
    ticks = []

    async def run() -> None:
        stop = asyncio.Event()

        def tick():
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("gh down")
            stop.set()
            return {}

        monkeypatch.setattr(mg, "sweep_once", tick)
        await asyncio.wait_for(mg.merger_loop(stop=stop), timeout=5)

    asyncio.run(run())
    assert len(ticks) == 2, "a failed tick must not kill the loop"


# ── a failed branch fetch is not proof the branch is absent (#2586 review) ──


def _local_only_routes(ls_remote_rc: int) -> dict[str, CmdResult]:
    return {
        "fetch origin main": CmdResult(0, "", ""),
        "fetch origin aifactory/1": CmdResult(1, "", "fetch failed"),
        "ls-remote": CmdResult(ls_remote_rc, "", ""),
        "rev-parse --verify": CmdResult(0, "", ""),
        "rev-list --count": CmdResult(0, "2\n", ""),
        "diff --name-only": CmdResult(0, "a.py\n", ""),
    }


def test_branch_proven_absent_on_origin_measures_the_local_ref(tmp_path):
    r = FakeRunner(_local_only_routes(2))
    got = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert got == (2, 1, 2)


@pytest.mark.parametrize("rc", [0, 1, 128])
def test_fetch_failure_without_proof_of_absence_is_unmeasurable(tmp_path, rc):
    """A transient network/auth error must not send a possibly stale local ref
    to be measured -- that could label real work `no_work`."""
    r = FakeRunner(_local_only_routes(rc))
    got = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert got == (None, None, None)


def test_real_git_never_pushed_branch_is_measured_locally(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    wt = tmp_path / "wt"
    _git(tmp_path, "clone", "-q", str(origin), str(wt))
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    _git(wt, "checkout", "-qb", "main")
    _commit(wt, "base.txt")
    _git(wt, "push", "-q", "origin", "main")
    _git(wt, "checkout", "-qb", "aifactory/1")
    _commit(wt, "a.py")
    _commit(wt, "b.py")
    assert _measure(wt) == (2, 2, 2)
