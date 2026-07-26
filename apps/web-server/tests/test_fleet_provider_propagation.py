"""AIFactory honours the tenant's provider (RFC-0020 3.5, Factory#366).

The bug: a GitLab tenant's PARR run opened a GitHub pull request. AIFactory
chose its git host from its own configuration — a per-project ``gitProvider``
setting defaulting to ``github`` — and the PR endgame consulted no provider at
all, it shelled out to ``gh``. So the choice a tenant made in CFactory's
Settings panel had no effect on where the work actually landed.

Covered here:

* the task contract's repo reference decides the host, not the project default;
* the reference round-trips ``gitlab:group/project`` unchanged, and a clone URL
  is not shredded into a provider called ``https``;
* a GitHub tenant is byte-for-byte unaffected;
* ``gather_pr_context`` reports the declared provider and a BARE repo path;
* the GitHub-shaped auto-PR endgame REFUSES off GitHub, loudly.

**Mutation guard (b).** ``test_a_gitlab_tenant_cannot_reach_the_github_auto_pr_path``
is the one that fails if a GitLab tenant is allowed down the auto-PR path:
delete the ``_is_github`` guard in ``run_pr_endgame`` and it goes red, having
watched ``gh pr create`` run against a GitLab project.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from repo_ref import (  # noqa: E402
    is_github,
    parse_repo_ref,
    project_of,
    provider_of,
    qualify_repo,
)
from server.services import pr_endgame as pe  # noqa: E402
from test_pr_endgame import CmdResult, FakeRunner  # noqa: E402

_GL_REF = "gitlab:platform/pipelines"
_GH_REF = "acme/widgets"


# ── the reference ────────────────────────────────────────────────────────────


def test_the_contract_round_trips_a_gitlab_reference_unchanged():
    assert parse_repo_ref(_GL_REF) == ("gitlab", "platform/pipelines")
    assert qualify_repo(*parse_repo_ref(_GL_REF)) == _GL_REF


def test_github_is_the_unqualified_default_so_nothing_existing_changes():
    assert parse_repo_ref(_GH_REF) == ("github", _GH_REF)
    assert qualify_repo(*parse_repo_ref(_GH_REF)) == _GH_REF
    assert is_github(_GH_REF)
    assert is_github(None)


def test_azure_devops_qualifies_with_its_three_part_path():
    ref = "azure_devops:org/proj/repo"
    assert parse_repo_ref(ref) == ("azure_devops", "org/proj/repo")
    assert not is_github(ref)


def test_a_clone_url_is_not_shredded_into_a_provider_called_https():
    """Splitting on the first colon regardless breaks the caller that worked."""
    url = "https://gitlab.example.com/platform/pipelines.git"
    assert parse_repo_ref(url) == ("github", url)
    assert project_of(url) == url


def test_an_unimplemented_host_is_not_a_qualification():
    """bitbucket/gitea are declared in the protocol and unimplemented.

    Treating one as a qualification would route work to a client that only ever
    raises, which is strictly worse than treating it as part of the path.
    """
    assert parse_repo_ref("bitbucket:team/repo") == ("github", "bitbucket:team/repo")


def test_an_absent_reference_falls_back_but_a_present_one_never_does():
    """The precedence that makes the fix a fix.

    An absent reference may fall back to a caller's default. A reference that is
    PRESENT but unqualified means GitHub — letting a default override it is the
    exact behaviour Factory#366 removes.
    """
    assert provider_of(None, default="gitlab") == "gitlab"
    assert provider_of("", default="gitlab") == "gitlab"
    assert provider_of(_GH_REF, default="gitlab") == "github"


# ── the auto-PR path ─────────────────────────────────────────────────────────


def _context(
    tmp_path: Path, spec_id: str, requirements: dict
) -> tuple[Path, FakeRunner]:
    wt = tmp_path / ".aifactory" / "worktrees" / "tasks" / spec_id
    wt.mkdir(parents=True)
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    (spec_dir / "requirements.json").write_text(json.dumps(requirements))
    return spec_dir, FakeRunner({"rev-parse": CmdResult(0, f"aifactory/{spec_id}", "")})


def test_gather_pr_context_reports_the_declared_provider(tmp_path):
    """And a BARE repo path: `gh` and _split_repo both want owner/name."""
    spec_dir, runner = _context(tmp_path, "spec-gl", {"github_repo": _GL_REF})
    ctx = pe.gather_pr_context(tmp_path, spec_dir, "spec-gl", runner=runner)
    assert ctx is not None
    assert ctx["provider"] == "gitlab"
    assert ctx["repo"] == "platform/pipelines"
    # The failure this rules out: an unparsed "gitlab:platform/pipelines" reaching
    # _split_repo yields the owner "gitlab:platform", a plausible-looking GitHub
    # repo that does not exist.
    assert not ctx["repo"].startswith("gitlab:")


def test_gather_pr_context_is_unchanged_for_a_github_tenant(tmp_path):
    spec_dir, runner = _context(tmp_path, "spec-gh", {"github_repo": _GH_REF})
    ctx = pe.gather_pr_context(tmp_path, spec_dir, "spec-gh", runner=runner)
    assert ctx is not None
    assert ctx["provider"] == "github"
    assert ctx["repo"] == _GH_REF


@pytest.mark.parametrize("provider", ["gitlab", "azure_devops"])
def test_a_gitlab_tenant_cannot_reach_the_github_auto_pr_path(provider):
    """MUTATION GUARD (b): the reported bug, asserted.

    The endgame is gh-CLI-driven end to end — create, request a Copilot review,
    merge — and the canonical GitLab/Azure DevOps providers raise
    NotImplementedError for enable_auto_merge anyway. A non-GitHub tenant must be
    refused BEFORE any of it runs.

    The runner is armed to succeed at everything. So if the guard is removed,
    this does not merely fail: it observes `gh pr create` being run against a
    project that is not on GitHub, which is precisely the user-facing bug.
    """
    runner = FakeRunner(
        {
            "git push": CmdResult(0, "", ""),
            "pr create": CmdResult(0, "https://github.com/o/r/pull/1", ""),
            "pr merge": CmdResult(0, "merged", ""),
        }
    )
    res = asyncio.run(
        pe.run_pr_endgame(
            spec_dir=Path("/tmp"),
            spec_id="spec-1",
            worktree=Path("/tmp"),
            branch="aifactory/spec-1",
            base="main",
            repo="platform/pipelines",
            provider=provider,
            auto_merge=True,
            # Not needed to pass — the guard returns long before any review. It
            # is here so that REMOVING the guard fails in seconds instead of
            # hanging: without it the mutated path opens the PR and enters the
            # verdict watch, which never resolves, so a regression would burn a
            # CI job to its timeout rather than going red on the assertion below.
            review_fn=lambda: pe.ReviewState("approved"),
            runner=runner,
            background=False,
        )
    )
    assert res["ok"] is False
    assert res["reason"] == "provider_not_github"
    assert res["provider"] == provider
    # Refused BEFORE the network, not after: no gh command ran at all.
    assert not runner.saw("pr create")
    assert not runner.saw("pr merge")
    assert not runner.saw("git push")


def test_a_github_tenant_still_gets_its_auto_pr():
    """The refusal must not cost the case that always worked."""
    runner = FakeRunner(
        {
            "git push": CmdResult(0, "", ""),
            "pr create": CmdResult(0, "https://github.com/o/r/pull/7", ""),
            "pr merge": CmdResult(0, "merged", ""),
        }
    )
    res = asyncio.run(
        pe.run_pr_endgame(
            spec_dir=Path("/tmp"),
            spec_id="spec-2",
            worktree=Path("/tmp"),
            branch="aifactory/spec-2",
            base="main",
            repo="o/r",
            provider="github",
            auto_merge=True,
            review_fn=lambda: pe.ReviewState("approved"),
            runner=runner,
            background=False,
        )
    )
    assert res["ok"] is True
    assert res["pr"] == 7
    assert runner.saw("pr create")


def test_the_provider_argument_defaults_to_github():
    """No existing call site changes: `provider` is keyword-with-a-default.

    Asserted off the signature rather than by running the endgame — the GitHub
    path is already covered above, and this claim is about the parameter, not
    about a second trip through the watch loop.
    """
    import inspect

    param = inspect.signature(pe.run_pr_endgame).parameters["provider"]
    assert param.default == "github"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
