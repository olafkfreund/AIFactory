"""Tests for the merger (services.merger) -- see that module's docstring.

The merger opens PRs for stranded task branches (real commits, pushed, no open
PR) without requiring QA approval. Every git/gh call goes through an injectable
runner (reused from ``pr_endgame``), so these tests touch no network/git.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

from server.services import merger as mg  # noqa: E402
from server.services.pr_endgame import CmdResult  # noqa: E402


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


# ── _find_open_pr ────────────────────────────────────────────────────────────


def test_find_open_pr_none():
    r = FakeRunner({"pr list": CmdResult(0, "", "")})
    assert mg._find_open_pr("o", "r", "b", r) == (True, None)


def test_find_open_pr_found():
    r = FakeRunner({"pr list": CmdResult(0, "42\n", "")})
    assert mg._find_open_pr("o", "r", "b", r) == (True, 42)


def test_find_open_pr_query_failure_is_unmeasured_not_none():
    """Finding #1: a failed `gh pr list` must be distinguishable from a
    successful query that found nothing -- else the sweep proceeds to
    `gh pr create` on an idempotency check that was never actually made."""
    r = FakeRunner({"pr list": CmdResult(1, "", "rate limited")})
    assert mg._find_open_pr("o", "r", "b", r) == (False, None)


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
    ahead, changed = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert (ahead, changed) == (3, 2)


def test_branch_ahead_and_changed_unmeasurable_when_branch_not_on_origin(tmp_path):
    r = FakeRunner(
        {
            "fetch origin main": CmdResult(0, "", ""),
            "fetch origin aifactory/1": CmdResult(1, "", "couldn't find remote ref"),
        }
    )
    ahead, changed = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert (ahead, changed) == (None, None)


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
    ahead, changed = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert (ahead, changed) == (0, 0)


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
    ahead, changed = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert (ahead, changed) == (None, None)


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
    ahead, changed = mg._branch_ahead_and_changed(tmp_path, "main", "aifactory/1", r)
    assert (ahead, changed) == (None, None)


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
    assert out == {"task": "proj:001-x", "action": "opened", "pr": 5, "reason": None}
    assert r.saw("pr create")


def test_process_spec_idempotent_when_pr_already_open(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    spec_dir = _spec(tmp_path, "001-x")
    r = FakeRunner(_routes(**{"pr list": CmdResult(0, "9\n", "")}))
    out = mg._process_spec("proj", tmp_path, spec_dir, dry_run=False, runner=r)
    assert out == {
        "task": "proj:001-x",
        "action": "already_open",
        "pr": 9,
        "reason": None,
    }
    assert not r.saw("pr create"), "an already-open PR must never be re-opened"


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
        _routes(**{"fetch origin aifactory/001-x": CmdResult(1, "", "no such ref")})
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

    def fake_process(project_id, _project_path, spec_dir, *, dry_run, runner):
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
